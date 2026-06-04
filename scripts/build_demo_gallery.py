#!/usr/bin/env python3
"""Build the precomputed demo gallery for /gallery.

Generates a small set of ActivationPayload JSON files plus an index.json
manifest under apps/web/public/demo-predictions/. The gallery page fetches
these directly — no backend, no network, no HuggingFace, no AWS. This is
the Tier-3 fallback that keeps a usable product running at $0 forever.

Each entry is a (YouTube URL, startSec, endSec) segment per CONTRACTS.md
§13; only that window is analyzed. For each entry:

    1. Try `python scripts/predict_one_url.py <url> --start-sec S --end-sec E
       --output <path>`. If the predictor CLI is on disk, its output (real
       or mock) is what we commit and `modelVersion` flows from the CLI.
    2. Else, fall back to running `MockBackend` directly here. The output
       is marked with the sentinel modelVersion `"tribe-v2-mock-gallery"`
       so it's obvious in DevTools which entries are real vs synthesised.

Re-run anytime — the script is idempotent; outputs overwrite. The JSON
schema is the ActivationPayload that apps/web/lib/api-v2.ts validates,
so the same files feed both `<BrainMesh />` directly and the api-v2
`fetchActivation` codepath if we ever wire the gallery through it.

Usage:
    python scripts/build_demo_gallery.py [--mock-only]
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Repo layout
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
PREDICT_CLI = REPO_ROOT / "scripts" / "predict_one_url.py"
OUTPUT_DIR = REPO_ROOT / "apps" / "web" / "public" / "demo-predictions"
MOCK_MODEL_VERSION = "tribe-v2-mock-gallery"

# Inference + pipeline imports get resolved via PYTHONPATH (the Makefile sets
# this for `make` invocations; we also fall back to direct sys.path injection
# for `python scripts/build_demo_gallery.py`). `neural_media_pipeline` is on
# the path for the `--dry-run` validators (is_supported_youtube_url /
# validate_segment), which are imported lazily so `--help` needs no deps.
sys.path.insert(0, str(REPO_ROOT / "services" / "pipeline"))
sys.path.insert(0, str(REPO_ROOT / "services" / "inference"))
sys.path.insert(0, str(REPO_ROOT))

# Imported lazily inside _mock_predict so a missing numpy install doesn't
# kill the CLI path. Keeping the import at module top is fine in practice
# because numpy is a hard dep of services/inference, but lazy load keeps
# this script honest about which code path actually needs it.


# ---------------------------------------------------------------------------
# Curated entries
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class DemoEntry:
    label: str
    url: str
    # CONTRACTS.md §13: only the [startSec, endSec) window is analyzed.
    # endSec - startSec is the clip length that flows into MockBackend and
    # becomes `videoDurationSec`. Realistic short-clip range is 12–45s
    # (hard cap 90s). With MockBackend the activations are deterministically
    # seeded off (url, seed), so the window choice shapes the rendered look.
    startSec: float = 0.0
    endSec: float | None = None
    # Legacy convenience: a bare `duration_sec=N` (no endSec) is treated as
    # the window [startSec, startSec + N). Prefer startSec/endSec.
    duration_sec: dataclasses.InitVar[float | None] = None

    def __post_init__(self, duration_sec: float | None) -> None:
        if self.endSec is None:
            if duration_sec is None:
                raise ValueError("DemoEntry needs endSec (or legacy duration_sec)")
            object.__setattr__(self, "endSec", self.startSec + duration_sec)

    @property
    def analyzed_duration_sec(self) -> float:
        """Clip length actually analyzed — endSec - startSec (§13.3)."""
        return self.endSec - self.startSec


# ─────────────────────────────────────────────────────────────────────────
# TODO(brain): replace these 8 PLACEHOLDERS with the real clip list.
#
# Each URL id is the obvious sentinel "REPLACE_ME_0N" — NOT a valid 11-char
# YouTube id — so `python scripts/build_demo_gallery.py --dry-run` FAILS
# (invalid_url) until you fill in real clips. That failure is the gate that
# stops a half-edited list reaching the (expensive) GPU bake.
#
# Replace each entry with a real (YouTube URL, startSec, endSec), keeping
# 0 <= startSec < endSec and endSec - startSec <= 90, and pick varied content
# (visual / faces / on-screen text / music) so the brain maps differ
# clip-to-clip. Format:
#
#     DemoEntry(
#         label="NASA — Aurora over the Arctic",
#         url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",   # real 11-char id
#         startSec=12.0, endSec=30.0,                          # end-start <= 90
#     ),
#
# After editing, `--dry-run` must print all-PASS before you bake for real.
# ─────────────────────────────────────────────────────────────────────────
DEMO_ENTRIES: tuple[DemoEntry, ...] = (
    DemoEntry(
        label="NASA — Aurora over the Arctic",
        url="https://www.youtube.com/watch?v=REPLACE_ME_01",
        startSec=5.0, endSec=23.0,
    ),
    DemoEntry(
        label="NASA — Mars dust storm timelapse",
        url="https://www.youtube.com/watch?v=REPLACE_ME_02",
        startSec=10.0, endSec=32.0,
    ),
    DemoEntry(
        label="NatGeo — Octopus camouflage",
        url="https://www.youtube.com/watch?v=REPLACE_ME_03",
        startSec=0.0, endSec=15.0,
    ),
    DemoEntry(
        label="NatGeo — Lions at sunset",
        url="https://www.youtube.com/watch?v=REPLACE_ME_04",
        startSec=8.0, endSec=35.0,
    ),
    DemoEntry(
        label="BBC Earth — Hummingbird (slow-mo)",
        url="https://www.youtube.com/watch?v=REPLACE_ME_05",
        startSec=2.0, endSec=14.5,
    ),
    DemoEntry(
        label="Khan Academy — Pythagoras visual proof",
        url="https://www.youtube.com/watch?v=REPLACE_ME_06",
        startSec=0.0, endSec=33.0,
    ),
    DemoEntry(
        label="Smithsonian — T-Rex skull tour",
        url="https://www.youtube.com/watch?v=REPLACE_ME_07",
        startSec=12.0, endSec=32.0,
    ),
    DemoEntry(
        label="Duolingo — 'thank you' in five languages",
        url="https://www.youtube.com/watch?v=REPLACE_ME_08",
        startSec=0.0, endSec=14.0,
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SLUG_RX = re.compile(r"[^a-z0-9]+")


def slugify(label: str) -> str:
    s = _SLUG_RX.sub("-", label.lower()).strip("-")
    return s or "demo"


def _video_id_from_url(url: str) -> str:
    """Best-effort stable video id for seeding MockBackend.

    Extracts the YouTube id (watch?v=…, youtu.be/…, /shorts/…); falls back
    to the last path segment or the raw URL so the seed mixer always gets
    *something* unique and stable.
    """
    try:
        parts = urllib.parse.urlparse(url)
    except ValueError:
        return url
    host = (parts.hostname or "").lower()
    if host == "youtu.be":
        tail = parts.path.lstrip("/").split("/", 1)[0]
        if tail:
            return tail
    if host.endswith("youtube.com"):
        if parts.path == "/watch":
            v = urllib.parse.parse_qs(parts.query or "").get("v", [""])[0]
            if v:
                return v
        if parts.path.startswith("/shorts/"):
            tail = parts.path[len("/shorts/"):].split("/", 1)[0]
            if tail:
                return tail
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return tail or url


# ---------------------------------------------------------------------------
# Prediction paths
# ---------------------------------------------------------------------------

def _try_real_cli(entry: DemoEntry, output_path: Path) -> tuple[bool, str | None]:
    """Try the predictor CLI on the entry's segment. Returns (ok, error).
    On ok=True the file at output_path is the prediction JSON; on ok=False
    we'll fall back to the local mock."""
    if not PREDICT_CLI.exists():
        return False, "predict_one_url.py not present"
    try:
        result = subprocess.run(
            [
                sys.executable, str(PREDICT_CLI), entry.url,
                "--start-sec", str(entry.startSec),
                "--end-sec", str(entry.endSec),
                "--output", str(output_path),
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return False, "predict_one_url.py timed out (300s)"
    if result.returncode != 0:
        # Don't leak the full stderr into the manifest — keep a short tag.
        return False, f"predict_one_url.py exited {result.returncode}"
    if not output_path.exists():
        return False, "predict_one_url.py did not write output"
    return True, None


def _mock_predict(entry: DemoEntry, output_path: Path) -> dict[str, Any]:
    """Run MockBackend + downsample_region_means + ActivationPayload-shape
    JSON write. Returns the payload dict for manifest assembly."""
    # Import here so a stripped-down environment can still print --help.
    from neural_media_inference import (
        WIRE_TIMESTAMP_DECIMALS,
        MockBackend,
        downsample_region_means,
    )

    backend = MockBackend()
    # The analyzed length is the segment span (endSec - startSec, §13.3).
    duration = entry.analyzed_duration_sec
    # 2 Hz native sample rate keeps the raw tensor small (~36 frames at 18s)
    # while still giving the downsampler something to pool. Hard-code the
    # seed so the gallery's "look" is reproducible.
    activations = backend.infer(
        video_id=_video_id_from_url(entry.url),
        duration_s=duration,
        seed=42,
        sample_rate_hz=2.0,
    )
    by_region = downsample_region_means(activations, max_timepoints=30)
    series_len = len(next(iter(by_region.values())))

    # Evenly-spaced timestamps over the clip. The api-v2 validator checks
    # len(timestamps) == len(byRegion[region]) for every region, so this
    # must be exactly series_len long. Rounded to the shared
    # WIRE_TIMESTAMP_DECIMALS so this matches predict_one_url.py's precision.
    if series_len == 1:
        timestamps = [0.0]
    else:
        step = duration / (series_len - 1)
        timestamps = [round(i * step, WIRE_TIMESTAMP_DECIMALS) for i in range(series_len)]

    payload: dict[str, Any] = {
        "videoDurationSec": duration,
        "timestamps": timestamps,
        "byRegion": by_region,
        "modelVersion": MOCK_MODEL_VERSION,
    }
    _atomic_write_json(output_path, payload)
    return payload


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def _read_existing_payload(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build(force_mock: bool) -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    real = 0
    mock = 0

    for entry in DEMO_ENTRIES:
        slug = slugify(entry.label)
        output_path = OUTPUT_DIR / f"{slug}.json"

        used_real = False
        cli_skip_reason: str | None = None
        if not force_mock:
            ok, err = _try_real_cli(entry, output_path)
            if ok:
                used_real = True
            else:
                cli_skip_reason = err

        payload: dict[str, Any]
        if used_real:
            existing = _read_existing_payload(output_path)
            if existing is None:
                # CLI claimed success but file unreadable — defensive fallback.
                payload = _mock_predict(entry, output_path)
                used_real = False
                cli_skip_reason = "CLI output unreadable"
            else:
                payload = existing
        else:
            payload = _mock_predict(entry, output_path)

        if used_real:
            real += 1
        else:
            mock += 1

        manifest.append({
            "slug": slug,
            "label": entry.label,
            "url": entry.url,
            "startSec": entry.startSec,
            "endSec": entry.endSec,
            "durationSec": payload.get("videoDurationSec", entry.analyzed_duration_sec),
            "modelVersion": payload.get("modelVersion", MOCK_MODEL_VERSION),
        })

        tag = "real" if used_real else f"mock ({cli_skip_reason or 'forced'})"
        print(f"  [{tag:32s}] {slug}.json", flush=True)

    _atomic_write_json(OUTPUT_DIR / "index.json", {"entries": manifest})

    try:
        where: Path | str = OUTPUT_DIR.relative_to(REPO_ROOT)
    except ValueError:
        where = OUTPUT_DIR  # e.g. a tmp dir under test, outside the repo
    print()
    print(f"wrote {len(manifest)} predictions to {where}/")
    print(f"  real: {real}   mock: {mock}")
    return 0


def dry_run(entries: tuple[DemoEntry, ...] = DEMO_ENTRIES) -> int:
    """Validate every entry against CONTRACTS.md §13 with NO GPU/network.

    Uses the *same* validators the real download path enforces
    (``is_supported_youtube_url`` + ``validate_segment``), so a PASS here
    means the bake won't be rejected for a bad URL or segment. Prints a
    per-entry PASS/FAIL table; returns 1 if any entry fails, else 0.
    """
    # Lazy import keeps `--help` dependency-free; pulled from the pipeline
    # package so the dry-run gate matches the real download-time checks.
    from neural_media_pipeline.downloader import (
        SegmentError,
        is_supported_youtube_url,
        validate_segment,
    )

    print(f"{'STATUS':<6}  {'LABEL':<42}  {'SEGMENT':>14}  REASON")
    print("-" * 78)
    failures = 0
    for entry in entries:
        reasons: list[str] = []
        if not is_supported_youtube_url(entry.url):
            reasons.append("invalid_url")
        try:
            validate_segment(entry.startSec, entry.endSec)
        except SegmentError as exc:
            reasons.append(exc.code)
        ok = not reasons
        failures += 0 if ok else 1
        seg = f"[{entry.startSec:g},{entry.endSec:g})"
        print(f"{'PASS' if ok else 'FAIL':<6}  {entry.label[:42]:<42}  "
              f"{seg:>14}  {'ok' if ok else ', '.join(reasons)}")
    print("-" * 78)
    total = len(entries)
    print(f"{total - failures}/{total} PASS")
    if failures:
        plural = "y" if failures == 1 else "ies"
        print(f"\n{failures} entr{plural} FAILED — fix the clip list (see the "
              "TODO block in this file) before baking for real.")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mock-only",
        action="store_true",
        help="Skip the real CLI even if it exists. Useful for reproducible "
             "checked-in gallery output.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the clip list (YouTube URL + segment) against the "
             "contract with NO GPU/network. Prints a PASS/FAIL table and "
             "exits nonzero on any failure. Run this before the real bake.",
    )
    args = parser.parse_args()
    if args.dry_run:
        return dry_run()
    return build(force_mock=args.mock_only)


if __name__ == "__main__":
    raise SystemExit(main())
