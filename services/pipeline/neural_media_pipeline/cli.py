"""Command-line entry point for the data pipeline.

Usage:
    python -m neural_media_pipeline path/to/user_data.json [--flags]

Defaults assume the standard repo-root layout (``data/sqlite``,
``data/videos``, ``data/videos_processed``, ``data/activations``).
The ``--data-root`` flag overrides all of them at once.

The CLI prints only video ids — never source URLs — so its output is
safe to paste into bug reports.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .orchestrate import (
    IngestSummary,
    Orchestrator,
    OrchestratorConfig,
)

_log = logging.getLogger("neural_media_pipeline")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="neural_media_pipeline",
        description="Ingest a TikTok export and drive download → preprocess → infer.",
    )
    p.add_argument(
        "export",
        nargs="?",
        type=Path,
        help="Path to TikTok user_data.json. Omit with --run-pending to drive "
             "existing queued jobs without re-importing.",
    )
    p.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help="Overrides --db, --videos-dir, --processed-dir, --activations-dir "
             "to subdirs of this root. Default: repo data/ next to the package.",
    )
    p.add_argument("--db", type=Path, default=None,
                   help="SQLite path. Default: <data-root>/sqlite/neural_media.db")
    p.add_argument("--videos-dir", type=Path, default=None,
                   help="Where downloads land. Default: <data-root>/videos")
    p.add_argument("--processed-dir", type=Path, default=None,
                   help="Where preprocessed mp4s land. Default: <data-root>/videos_processed")
    p.add_argument("--activations-dir", type=Path, default=None,
                   help="Where activation npz + sidecar land. Default: <data-root>/activations")
    p.add_argument("--run-pending", action="store_true",
                   help="Skip the export parse; just drive queued/failed jobs.")
    p.add_argument("--dry-run", action="store_true",
                   help="Parse the export and print counts only — no downloads.")
    p.add_argument("-v", "--verbose", action="count", default=0,
                   help="-v for INFO, -vv for DEBUG.")
    return p


def _default_data_root() -> Path:
    """Walk up from this file until we find a ``data/`` directory.

    Mirrors the layout used by every other service in the repo. Falls
    back to ``./data`` (created on demand) if nothing matches.
    """
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        candidate = parent / "data"
        if candidate.is_dir() and (candidate / "sqlite").exists():
            return candidate
    return Path.cwd() / "data"


def _resolve_paths(args: argparse.Namespace) -> OrchestratorConfig:
    root = args.data_root or _default_data_root()
    return OrchestratorConfig(
        db_path=args.db or root / "sqlite" / "neural_media.db",
        videos_dir=args.videos_dir or root / "videos",
        processed_dir=args.processed_dir or root / "videos_processed",
        activations_dir=args.activations_dir or root / "activations",
    )


def _print_summary(summary: IngestSummary) -> None:
    print(
        f"parsed: {summary.parsed_videos} videos, {summary.parsed_events} events; "
        f"queued: {summary.queued}; "
        f"completed: {summary.completed}; failed: {summary.failed}"
    )
    for video_id, msg in summary.errors[:10]:
        print(f"  failed {video_id}: {msg}", file=sys.stderr)
    if len(summary.errors) > 10:
        print(f"  ... and {len(summary.errors) - 10} more", file=sys.stderr)


def _configure_logging(verbosity: int) -> None:
    level = {0: logging.WARNING, 1: logging.INFO}.get(verbosity, logging.DEBUG)
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    _configure_logging(args.verbose)

    if not args.export and not args.run_pending:
        print("error: provide an export path or use --run-pending", file=sys.stderr)
        return 2
    if args.export and not args.export.is_file():
        print(f"error: export file not found: {args.export}", file=sys.stderr)
        return 2

    cfg = _resolve_paths(args)

    if args.dry_run:
        from .importer import parse_export
        videos, events = parse_export(args.export)  # type: ignore[arg-type]
        summary = IngestSummary(parsed_videos=len(videos), parsed_events=len(events))
        _print_summary(summary)
        return 0

    with Orchestrator(cfg) as orch:
        if args.run_pending:
            summary = orch.run_pending()
        else:
            summary = orch.ingest_export(args.export)

    _print_summary(summary)
    return 1 if summary.failed and not summary.completed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
