"""Observability payload for ``GET /api/v1/debug``.

Read-only snapshot of host-side state useful for debugging a local
install: app version, on-disk paths, row counts in the catalog DB, the
most recent import job, real-mode capability probe, and rough disk
usage. Intended for the local UI's diagnostics panel and for hand
inspection via curl — no auth, no caching, loopback-only like the rest
of the API.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .import_jobs import ImportJobsStore, real_mode_capabilities

# Module-load time stands in for app boot. The API process and this
# module share a lifetime — there's no scenario in which we'd re-import
# debug.py without also re-creating the FastAPI app.
APP_BOOT_TIME = time.monotonic()


def get_uptime() -> float:
    """Seconds since ``APP_BOOT_TIME`` was sampled."""
    return time.monotonic() - APP_BOOT_TIME


_COUNT_TABLES: tuple[str, ...] = (
    "videos",
    "watch_events",
    "inference_runs",
    "import_jobs",
)


def _count_rows(db_path: Path, table: str) -> int:
    """``SELECT COUNT(*)`` that swallows missing-file / missing-table.

    The debug endpoint must never 500 just because the catalog hasn't
    been populated yet — return 0 in that case, matching the observable
    behavior of an empty DB.
    """
    if not db_path.is_file():
        return 0
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()
    return int(row[0]) if row else 0


def _dir_size(path: Path) -> int:
    """Recursive sum of ``st_size`` for every file under ``path``.

    Returns 0 if the directory doesn't exist. Symlinks are followed by
    ``rglob`` but ``stat()`` on a broken symlink raises — guard so a
    single dangling link doesn't sink the whole endpoint.
    """
    if not path.is_dir():
        return 0
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def _file_size(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0


def build_debug_payload(
    *,
    version: str,
    db_path: Path,
    videos_dir: Path,
    activations_dir: Path,
    imports_dir: Path,
    jobs_store: ImportJobsStore,
) -> dict:
    """Compose the dict the route returns.

    All paths resolve to absolute strings so the response is portable
    enough to paste into a bug report without ambiguity. Counts /
    sizes / uptime are sampled lazily on each call.
    """
    latest = jobs_store.latest()
    # ``model_dump(mode="json")`` turns datetimes into ISO strings so
    # the FastAPI default JSON encoder doesn't have to special-case
    # them inside a free-form dict.
    latest_import = latest.model_dump(mode="json") if latest is not None else None

    ok, blockers = real_mode_capabilities()
    capabilities = {
        "mock": True,
        "real": ok,
        "real_blockers": blockers,
    }

    counts = {table: _count_rows(db_path, table) for table in _COUNT_TABLES}

    disk_usage = {
        "videos": _dir_size(videos_dir),
        "activations": _dir_size(activations_dir),
        "imports": _dir_size(imports_dir),
        "sqlite": _file_size(db_path),
    }

    return {
        "version": version,
        "db_path": str(db_path.resolve()),
        "videos_dir": str(videos_dir.resolve()),
        "counts": counts,
        "latest_import": latest_import,
        "capabilities": capabilities,
        "disk_usage": disk_usage,
        "uptime_s": get_uptime(),
    }


__all__ = ["APP_BOOT_TIME", "build_debug_payload", "get_uptime"]
