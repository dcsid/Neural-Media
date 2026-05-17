"""One-off probe: run ``_yt_dlp_fetch`` against a single tiktokv.com share URL.

The newer TikTok export emits share-shortlinks like
``https://www.tiktokv.com/share/video/<id>/`` rather than the canonical
``https://www.tiktok.com/@<handle>/video/<id>`` form. yt-dlp's TikTok
extractor is supposed to follow the share-host redirect, but we hadn't
exercised that path end-to-end. This script does that exercise and
prints a one-line verdict so the result can be pasted into a commit
message or the downloader docstring.

Usage::

    python services/pipeline/scripts/probe_share_url.py \\
        https://www.tiktokv.com/share/video/7640163791312801054/

With no positional arg the script pulls the first ``Link:`` from
``Watch History.txt`` at the standard download location.

This is a network call. It hits TikTok exactly once. Privacy: the URL
is printed (only an id, no PII) but the resolved title / uploader handle
is *not* logged.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError

# Importing here rather than at the top so missing yt-dlp surfaces a
# friendly error from main() instead of an import-time crash.

DEFAULT_HISTORY = Path.home() / "Downloads" / "TikTok" / "Your Activity" / "Watch History.txt"
_LINK_RE = re.compile(r"^Link:\s*(\S+)", re.MULTILINE)


def _first_share_url_from_history(path: Path) -> str | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    match = _LINK_RE.search(text)
    return match.group(1) if match else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Probe yt-dlp against a TikTok share-shortlink URL."
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=None,
        help="A share URL like https://www.tiktokv.com/share/video/<id>/. "
             f"Defaults to the first Link: in {DEFAULT_HISTORY}.",
    )
    args = parser.parse_args(argv)

    url = args.url or _first_share_url_from_history(DEFAULT_HISTORY)
    if not url:
        print(f"no URL supplied and {DEFAULT_HISTORY} has none", file=sys.stderr)
        return 2

    try:
        from neural_media_pipeline.downloader import _yt_dlp_fetch
    except ImportError as exc:
        print(f"failed to import _yt_dlp_fetch: {exc}", file=sys.stderr)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="nm-probe-"))
    dest = tmp / "probe.mp4"
    user_agent = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    )
    print(f"probe: {url}")
    print(f"  writing to: {dest}")
    try:
        _yt_dlp_fetch(url, dest, user_agent)
    except HTTPError as exc:
        print(f"  HTTPError {exc.code}: {exc.reason}")
        return 1
    except URLError as exc:
        print(f"  URLError: {exc.reason}")
        return 1
    except Exception as exc:  # noqa: BLE001 — yt_dlp raises many shapes
        print(f"  {type(exc).__name__}: {exc}")
        return 1

    if dest.exists() and dest.stat().st_size > 0:
        # We don't sniff the container deeply — yt-dlp's selection above
        # forces mp4/m4a-best, so existence is the signal that matters.
        print(f"  OK: downloaded {dest.stat().st_size} bytes")
        return 0
    print("  yt-dlp returned without raising but produced no file")
    return 1


def _cleanup_tmp() -> None:
    # Called only by tests / interactive cleanup; main() leaves the
    # tmpdir behind so the user can inspect the artifact.
    for path in Path(tempfile.gettempdir()).glob("nm-probe-*"):
        shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
