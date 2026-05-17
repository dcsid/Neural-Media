"""Time mock-mode ingest of N synthetic videos.

Usage:
    PYTHONPATH=services/pipeline:services/api:services/inference \\
        .venv-dev/bin/python services/pipeline/scripts/bench_mock.py [N]

Reports wall-clock seconds and videos/sec. Used by ``make bench-ingest``
and by hand for ad-hoc validation. No cProfile (which serialises and
distorts the ProcessPoolExecutor wins).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _make_synthetic_export(path: Path, n: int) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    videos = [
        {
            "Date": (base + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S"),
            "Link": f"https://www.tiktok.com/@bench/video/{1000000 + i}",
        }
        for i in range(n)
    ]
    payload = {"Activity": {"Video Browsing History": {"VideoList": videos}}}
    path.write_text(json.dumps(payload))


def _run_one(
    *, n: int, workers: int | None, purge: bool, real_export: Path | None,
) -> tuple[float, int, int]:
    """Return ``(elapsed_s, completed, failed)`` for one ingest."""
    tmp = Path(tempfile.mkdtemp(prefix="nm-bench-"))
    try:
        if real_export is not None:
            export = real_export
        else:
            export = tmp / "user_data.json"
            _make_synthetic_export(export, n)

        from neural_media_pipeline import Orchestrator, OrchestratorConfig

        cfg = OrchestratorConfig(
            data_root=tmp,
            skip_download=True,
            skip_preprocess=True,
            purge_activations=purge,
            purge_after_inference=purge,
            parallel_workers=workers,
        )
        t0 = time.perf_counter()
        with Orchestrator(cfg) as orch:
            summary = orch.run(export)
        elapsed = time.perf_counter() - t0
        return elapsed, summary.completed, summary.failed
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("n", type=int, nargs="?", default=1000,
                   help="Number of synthetic videos to ingest (default 1000).")
    p.add_argument("--workers", type=int, default=None,
                   help="Override parallel_workers. Omit for auto.")
    p.add_argument("--no-purge", action="store_true",
                   help="Keep activation artifacts on disk (slower; tests the "
                        "demo path that wants /v/[id] data live).")
    p.add_argument("--export", type=Path, default=None,
                   help="Use this real TikTok export instead of generating one. "
                        "``n`` is ignored when set.")
    p.add_argument("--repeat", type=int, default=1,
                   help="Repeat the run N times and report best wall time.")
    args = p.parse_args()

    times: list[float] = []
    for i in range(args.repeat):
        elapsed, done, failed = _run_one(
            n=args.n, workers=args.workers, purge=not args.no_purge,
            real_export=args.export,
        )
        n_label = done if args.export is None else done
        print(f"  run {i+1}/{args.repeat}: {elapsed:.2f}s  "
              f"({n_label / elapsed:.1f} vid/s)  "
              f"completed={done} failed={failed}")
        times.append(elapsed)

    best = min(times)
    n_completed = done  # last run's count — repeats are identical
    print(f"\nbest of {args.repeat}: {best:.2f}s  ({n_completed / best:.1f} vid/s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
