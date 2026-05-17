"""Profile mock-mode ingest of N synthetic videos.

Usage: PYTHONPATH=services/pipeline:services/api:services/inference \\
    .venv-dev/bin/python services/pipeline/scripts/profile_mock.py [N]

Default N=1000. Writes a synthetic user_data.json into a temp dir, runs
the orchestrator end-to-end in mock mode with the real run_inference +
MockBackend, and prints the top-30 cProfile hotspots by cumulative time.
"""
from __future__ import annotations

import cProfile
import json
import pstats
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


def make_export(path: Path, n: int) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    videos = []
    for i in range(n):
        when = base + timedelta(minutes=i)
        videos.append({
            "Date": when.strftime("%Y-%m-%d %H:%M:%S"),
            "Link": f"https://www.tiktok.com/@bench/video/{1000000 + i}",
        })
    payload = {
        "Activity": {
            "Video Browsing History": {"VideoList": videos}
        }
    }
    path.write_text(json.dumps(payload))


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    purge = "--no-purge" not in sys.argv

    tmp = Path(tempfile.mkdtemp(prefix="nm-bench-"))
    try:
        export = tmp / "user_data.json"
        make_export(export, n)

        from neural_media_pipeline import Orchestrator, OrchestratorConfig

        cfg = OrchestratorConfig(
            data_root=tmp,
            skip_download=True,
            skip_preprocess=True,
            purge_activations=purge,
            purge_after_inference=purge,
        )

        prof = cProfile.Profile()
        t0 = time.perf_counter()
        with Orchestrator(cfg) as orch:
            prof.enable()
            summary = orch.run(export)
            prof.disable()
        elapsed = time.perf_counter() - t0

        print(f"\n== {n} videos in {elapsed:.2f}s  "
              f"({n/elapsed:.1f} vid/s)  "
              f"completed={summary.completed} failed={summary.failed} ==\n")

        stats = pstats.Stats(prof).sort_stats("cumulative")
        stats.print_stats(30)
        print("\n-- by tottime --\n")
        pstats.Stats(prof).sort_stats("tottime").print_stats(20)
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
