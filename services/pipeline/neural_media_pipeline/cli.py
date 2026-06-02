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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .importer import MalformedExportError
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
    p.add_argument("--skip-download", action="store_true",
                   help="Demo mode: bypass yt-dlp; rows stay downloaded=false.")
    p.add_argument("--skip-preprocess", action="store_true",
                   help="Demo mode: bypass ffmpeg; mock-mode duration is synthesised.")
    p.add_argument("--mock", action="store_true",
                   help="Shortcut for --skip-download --skip-preprocess.")
    p.add_argument("--purge-after-inference", action="store_true",
                   help="After each video's RegionMetrics land in SQLite, "
                        "delete the raw + preprocessed mp4s. Keeps peak "
                        "disk to ~10 MB on large ingests but defeats the "
                        "yt-dlp dedup cache — re-runs hit TikTok again.")
    p.add_argument("--since", type=_parse_window_arg, default=None,
                   help="Drop events strictly before this UTC date/datetime "
                        "(YYYY-MM-DD or ISO-8601).")
    p.add_argument("--until", type=_parse_window_arg, default=None,
                   help="Drop events at or after this UTC date/datetime "
                        "(half-open upper bound).")
    p.add_argument("--days", type=int, default=None,
                   help="Convenience: keep only the last N days of history. "
                        "Equivalent to --since (now - N days). Overrides --since.")
    p.add_argument("--hours", type=int, default=None,
                   help="Convenience: keep only the last N hours of history. "
                        "Overrides --days and --since.")
    p.add_argument("--minutes", type=int, default=None,
                   help="Convenience: keep only the last N minutes of history. "
                        "Overrides --hours, --days, and --since.")
    p.add_argument("--purge-activations", action="store_true",
                   help="After each video's RegionMetrics land in SQLite, "
                        "delete the raw .npz, the .meta.json sidecar, and "
                        "the downsampled JSON payload. The catalogue keeps "
                        "the irreducible region readings; per-vertex mesh "
                        "playback is lost for purged videos.")
    p.add_argument("-v", "--verbose", action="count", default=0,
                   help="-v for INFO, -vv for DEBUG.")
    return p


def _parse_window_arg(raw: str) -> datetime:
    """argparse type for --since/--until.

    Accepts plain dates (``YYYY-MM-DD``) or full ISO-8601 timestamps.
    Naive values are stamped UTC, matching how the importer treats every
    other timestamp in this pipeline.
    """
    raw = raw.strip()
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"not a valid date/datetime: {raw!r} (expected YYYY-MM-DD or ISO-8601)"
        ) from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


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
    skip_d = args.skip_download or args.mock
    skip_p = args.skip_preprocess or args.mock
    return OrchestratorConfig(
        db_path=args.db or root / "sqlite" / "neural_media.db",
        videos_dir=args.videos_dir or root / "videos",
        processed_dir=args.processed_dir or root / "videos_processed",
        activations_dir=args.activations_dir or root / "activations",
        skip_download=skip_d,
        skip_preprocess=skip_p,
        purge_after_inference=args.purge_after_inference,
        purge_activations=args.purge_activations,
    )


def _resolve_since(args: argparse.Namespace) -> datetime | None:
    """Collapse --minutes / --hours / --days / --since into one cutoff.

    Finest unit wins so a user can pass several without surprises:
    ``--minutes 5 --days 30`` keeps only the last 5 minutes. ``--since``
    is the explicit lower bound and is honoured only when no relative
    flag is set.
    """
    now = datetime.now(timezone.utc)
    if args.minutes is not None:
        return now - timedelta(minutes=args.minutes)
    if args.hours is not None:
        return now - timedelta(hours=args.hours)
    if args.days is not None:
        return now - timedelta(days=args.days)
    return args.since


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
    if args.dry_run and args.export is None:
        # --dry-run parses an export and prints counts; it has nothing to do
        # without one (and would otherwise reach parse_export(None) below).
        print("error: --dry-run requires an export path", file=sys.stderr)
        return 2

    cfg = _resolve_paths(args)
    since = _resolve_since(args)
    until = args.until

    try:
        if args.dry_run:
            from .importer import parse_export
            assert args.export is not None  # guaranteed by the --dry-run guard above
            videos, events = parse_export(args.export, since=since, until=until)
            summary = IngestSummary(parsed_videos=len(videos), parsed_events=len(events))
            _print_summary(summary)
            return 0

        with Orchestrator(cfg) as orch:
            if args.run_pending:
                summary = orch.run_pending()
            else:
                summary = orch.run(args.export, since=since, until=until)
    except MalformedExportError as exc:
        # A corrupt/undecodable export is a clean, expected failure — print
        # the actionable message instead of a traceback.
        print(f"error: {exc}", file=sys.stderr)
        return 1

    _print_summary(summary)
    # Any failure is a non-zero exit — including a PARTIAL run (some videos
    # completed, some failed). Only a clean run (zero failures) exits 0.
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
