#!/usr/bin/env python3
"""Build the precomputed demo gallery for /single/gallery.

Generates a small set of ActivationPayload JSON files plus an index.json
manifest under apps/web/public/demo-predictions/. The gallery page fetches
these directly — no backend, no network, no HuggingFace, no AWS. This is
the Tier-3 fallback that keeps a usable product running at $0 forever.

For each curated entry:

    1. Try `python scripts/predict_one_url.py <url> --output <path>`. If
       worker T2's CLI is on disk, its output (real or mock) is what we
       commit and `modelVersion` flows from the CLI.
    2. Else, fall back to running `MockBackend` directly here. The output
       is marked with the sentinel modelVersion `"tribe-v2-mock-gallery"`
       so it's obvious in DevTools which entries are real vs synthesised.

Re-run anytime — the script is idempotent; outputs overwrite. The JSON
schema is the ActivationPayload that apps/web/lib/api-v2.ts validates,
so the same files feed both `<BrainMesh />` directly and the api-v2
`fetchActivation` codepath if we ever wire the gallery through it.

Usage:
    python scripts/build_demo_gallery.py
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Repo layout
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
PREDICT_CLI = REPO_ROOT / "scripts" / "predict_one_url.py"
OUTPUT_DIR = REPO_ROOT / "apps" / "web" / "public" / "demo-predictions"
MOCK_MODEL_VERSION = "tribe-v2-mock-gallery"

# Inference imports get resolved via PYTHONPATH (the Makefile sets this for
# `make` invocations; we also fall back to direct sys.path injection for
# `python scripts/build_demo_gallery.py`).
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
    # Suggested clip duration. With MockBackend, the activations are
    # deterministically seeded off (url, seed) so changing this value
    # changes the produced timeseries — pick a number you actually want
    # to render. Realistic short-clip range is 12–45s.
    duration_sec: float


# All URLs use clearly-synthetic 19-digit IDs starting with `1111…` so
# anyone tracing them sees these are demo placeholders and not real
# scrape targets. The accounts are real public/educational accounts
# whose general content shape these entries are meant to evoke.
DEMO_ENTRIES: tuple[DemoEntry, ...] = (
    DemoEntry(
        label="NASA — Aurora over the Arctic",
        url="https://www.tiktok.com/@nasa/video/1111111111111110001",
        duration_sec=18.0,
    ),
    DemoEntry(
        label="NASA — Mars dust storm timelapse",
        url="https://www.tiktok.com/@nasa/video/1111111111111110002",
        duration_sec=22.0,
    ),
    DemoEntry(
        label="NatGeo — Octopus camouflage",
        url="https://www.tiktok.com/@natgeo/video/1111111111111110003",
        duration_sec=15.0,
    ),
    DemoEntry(
        label="NatGeo — Lions at sunset",
        url="https://www.tiktok.com/@natgeo/video/1111111111111110004",
        duration_sec=27.0,
    ),
    DemoEntry(
        label="BBC Earth — Hummingbird (slow-mo)",
        url="https://www.tiktok.com/@bbcearth/video/1111111111111110005",
        duration_sec=12.5,
    ),
    DemoEntry(
        label="Khan Academy — Pythagoras visual proof",
        url="https://www.tiktok.com/@khan.academy/video/1111111111111110006",
        duration_sec=33.0,
    ),
    DemoEntry(
        label="Smithsonian — T-Rex skull tour",
        url="https://www.tiktok.com/@smithsonian/video/1111111111111110007",
        duration_sec=20.0,
    ),
    DemoEntry(
        label="Duolingo — 'thank you' in five languages",
        url="https://www.tiktok.com/@duolingo/video/1111111111111110008",
        duration_sec=14.0,
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
    # Last path segment is the video ID for the canonical TikTok URL shape;
    # fall back to the URL string if anything's odd so MockBackend's seed
    # mixer still gets *something* unique.
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return tail or url


# ---------------------------------------------------------------------------
# Prediction paths
# ---------------------------------------------------------------------------

def _try_real_cli(url: str, output_path: Path) -> tuple[bool, str | None]:
    """Try T2's CLI. Returns (ok, error). On ok=True the file at
    output_path is the prediction JSON; on ok=False we'll fall back."""
    if not PREDICT_CLI.exists():
        return False, "predict_one_url.py not present"
    try:
        result = subprocess.run(
            [sys.executable, str(PREDICT_CLI), url, "--output", str(output_path)],
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
    from neural_media_inference import MockBackend, downsample_region_means

    backend = MockBackend()
    # 2 Hz native sample rate keeps the raw tensor small (~36 frames at 18s)
    # while still giving the downsampler something to pool. Hard-code the
    # seed so the gallery's "look" is reproducible.
    activations = backend.infer(
        video_id=_video_id_from_url(entry.url),
        duration_s=entry.duration_sec,
        seed=42,
        sample_rate_hz=2.0,
    )
    by_region = downsample_region_means(activations, max_timepoints=30)
    series_len = len(next(iter(by_region.values())))

    # Evenly-spaced timestamps over the clip. The api-v2 validator checks
    # len(timestamps) == len(byRegion[region]) for every region, so this
    # must be exactly series_len long.
    if series_len == 1:
        timestamps = [0.0]
    else:
        step = entry.duration_sec / (series_len - 1)
        timestamps = [round(i * step, 3) for i in range(series_len)]

    payload: dict[str, Any] = {
        "videoDurationSec": entry.duration_sec,
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
            ok, err = _try_real_cli(entry.url, output_path)
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
            "durationSec": payload.get("videoDurationSec", entry.duration_sec),
            "modelVersion": payload.get("modelVersion", MOCK_MODEL_VERSION),
        })

        tag = "real" if used_real else f"mock ({cli_skip_reason or 'forced'})"
        print(f"  [{tag:32s}] {slug}.json", flush=True)

    _atomic_write_json(OUTPUT_DIR / "index.json", {"entries": manifest})

    print()
    print(f"wrote {len(manifest)} predictions to {OUTPUT_DIR.relative_to(REPO_ROOT)}/")
    print(f"  real: {real}   mock: {mock}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mock-only",
        action="store_true",
        help="Skip the real CLI even if it exists. Useful for reproducible "
             "checked-in gallery output.",
    )
    args = parser.parse_args()
    return build(force_mock=args.mock_only)


if __name__ == "__main__":
    raise SystemExit(main())
