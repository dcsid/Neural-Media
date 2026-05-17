"""Create the SQLite catalog DB if it doesn't already exist.

    python services/api/scripts/init_db.py [path]

Defaults to `data/sqlite/neural_media.db` relative to the repo root.
Idempotent — every statement uses ``CREATE ... IF NOT EXISTS``, so
re-running this on a populated DB is a no-op. The actual schema lives in
``neural_media_api.sqlite_store.SCHEMA_STATEMENTS`` so the API and the
script never drift.

Wire ``make init-db`` via terminal 1 after this lands.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
# Allow `python services/api/scripts/init_db.py` to work without
# `pip install -e services/api` having been run first — useful in CI
# bootstrap scripts.
sys.path.insert(0, str(_REPO_ROOT / "services" / "api"))

from neural_media_api.sqlite_store import init_db  # noqa: E402

DEFAULT_DB_PATH = _REPO_ROOT / "data" / "sqlite" / "neural_media.db"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"SQLite DB path (default: {DEFAULT_DB_PATH})",
    )
    args = parser.parse_args(argv)
    init_db(args.path)
    print(f"initialized: {args.path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
