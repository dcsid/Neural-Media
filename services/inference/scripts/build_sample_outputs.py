"""Regenerate `data/sample/mock_inference/` from the committed TikTok export.

Invoked by `make sample`. Reads the canonical sample export at
`data/sample/tiktok_export/user_data.json`, runs `MockBackend` for each
video, and writes three JSON files per video plus an `index.json`
manifest under `data/sample/mock_inference/`:

    {video_id}.run.json         InferenceRun
    {video_id}.metrics.json     list[RegionMetrics]
    {video_id}.activation.json  ActivationOutput (wire format — no raw 20k arrays)

NPZ + sidecar caches go to `data/sample/activations/`. That directory is
.gitignored — the JSON manifest is the committed contract.

Re-running this script is byte-equivalent across machines because run_id
and created_at are derived deterministically from (video_id, seed).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# The script is run from the repo root by `make sample`. Make it work
# regardless of cwd by walking up to the repo root explicitly.
_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "services" / "inference"))
sys.path.insert(0, str(_REPO_ROOT / "services" / "pipeline"))

from neural_media_inference import MockBackend, run_inference  # noqa: E402
# The canonical URL → video_id mapping lives in the data-pipeline package.
# Importing it here keeps the sample fixtures byte-aligned with whatever the
# real importer would produce against the same TikTok export. Previously
# this script used its own UUID namespace, which caused video_id drift
# (api-orchestrator saw 2 disjoint id-spaces and reported 16 videos for an
# 8-video export). Coordinated via the integration lead.
from neural_media_pipeline import stable_video_id  # noqa: E402

DEFAULT_EXPORT = _REPO_ROOT / "data" / "sample" / "tiktok_export" / "user_data.json"
DEFAULT_OUT_DIR = _REPO_ROOT / "data" / "sample" / "mock_inference"
DEFAULT_ACT_DIR = _REPO_ROOT / "data" / "sample" / "activations"
DEFAULT_SEED = 7
DEFAULT_SAMPLE_RATE_HZ = 1.5
DEFAULT_DURATION_S = 30.0

# UUID namespace for stable run_id derivation. video_id derivation is
# delegated to `neural_media_pipeline.stable_video_id` so the inference-
# side and pipeline-side id-spaces stay byte-identical. run_id is internal
# to this script (the real orchestrator allocates its own).
_RUN_ID_NAMESPACE = uuid.UUID("00000000-0000-0000-0000-000000005ee0")
# Pinned timestamp used for committed fixtures — keeps the JSON byte-stable
# across regenerations. Real runs use wallclock UTC.
_FIXTURE_CREATED_AT = datetime(2026, 5, 16, 0, 0, 0, tzinfo=timezone.utc)


def _iter_videos(export_path: Path):
    with export_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    history = (
        data.get("Activity", {})
            .get("Video Browsing History", {})
            .get("VideoList", [])
    )
    for entry in history:
        url = entry.get("Link")
        if not url:
            continue
        yield url


def _run_id_for(video_id: str, seed: int) -> str:
    return str(uuid.uuid5(_RUN_ID_NAMESPACE, f"run|{video_id}|{seed}"))


def _duration_for(video_id: str) -> float:
    """Spread synthetic durations across [15, 60] s so the timeseries
    lengths differ between videos. Deterministic in video_id."""
    h = int.from_bytes(hashlib.sha256(video_id.encode()).digest()[:4], "big")
    return 15.0 + (h % 46)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, default=DEFAULT_EXPORT)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--activations-dir", type=Path, default=DEFAULT_ACT_DIR)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--sample-rate-hz", type=float, default=DEFAULT_SAMPLE_RATE_HZ)
    parser.add_argument(
        "--duration-s",
        type=float,
        default=None,
        help="Override per-video duration. Default: deterministic in [15, 60].",
    )
    args = parser.parse_args(argv)

    if not args.export.exists():
        parser.error(f"export not found: {args.export}")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    args.activations_dir.mkdir(parents=True, exist_ok=True)

    backend = MockBackend()
    manifest = []
    for url in _iter_videos(args.export):
        video_id = stable_video_id(url)
        run_id = _run_id_for(video_id, args.seed)
        duration_s = args.duration_s if args.duration_s is not None else _duration_for(video_id)

        artifacts = run_inference(
            video_id=video_id,
            duration_s=duration_s,
            seed=args.seed,
            sample_rate_hz=args.sample_rate_hz,
            activations_dir=args.activations_dir,
            backend=backend,
            extra_params={"source_url": url},
            run_id=run_id,
            created_at=_FIXTURE_CREATED_AT,
        )

        (args.out_dir / f"{video_id}.run.json").write_text(
            artifacts.inference_run.model_dump_json(indent=2)
        )
        (args.out_dir / f"{video_id}.metrics.json").write_text(
            json.dumps(
                [m.model_dump() for m in artifacts.region_metrics],
                indent=2,
                default=str,
            )
        )
        (args.out_dir / f"{video_id}.activation.json").write_text(
            artifacts.activation_payload.model_dump_json(indent=2)
        )
        manifest.append({
            "video_id": video_id,
            "source_url": url,
            "inference_run_id": run_id,
            "duration_s": duration_s,
            "num_timepoints": artifacts.activation_payload.num_timepoints,
        })
        print(f"  wrote {video_id} ({duration_s:.1f}s, "
              f"T={artifacts.activation_payload.num_timepoints})")

    (args.out_dir / "index.json").write_text(
        json.dumps(
            {
                "model_id": backend.model_id,
                "model_version": backend.model_version,
                "seed": args.seed,
                "sample_rate_hz": args.sample_rate_hz,
                "videos": manifest,
            },
            indent=2,
        )
    )
    print(f"wrote {len(manifest)} video(s) to {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
