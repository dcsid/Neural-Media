"""Robustness tests for the ``Watch History.txt`` parser.

These tests document the parser's behaviour on malformed and edge-case
inputs that the demo fixture (and the smoke tests in
``test_importer.py``) don't exercise: unusual line endings, missing
trailing newlines, timezone-tagged dates, and very large inputs.

A regression here often means a TikTok export schema drift — they
periodically tweak the text format and our regex needs to keep up.
When that happens the failing test should be updated to reflect the
new shape, not the parser silently widened.

The parser under test lives in
``neural_media_pipeline/importer.py`` and is intentionally tolerant:
records it can't make sense of are dropped, not raised on.
"""

from __future__ import annotations

from datetime import timedelta, timezone
from pathlib import Path

from neural_media_pipeline.importer import parse_export


def test_txt_handles_crlf_line_endings(tmp_path: Path) -> None:
    """Windows-style ``\\r\\n`` separators must parse both records.

    The record regex explicitly allows an optional ``\\r`` before the
    newline between Date and Link, and ``finditer`` doesn't care what
    sits between records, so CRLF throughout is fine.
    """
    p = tmp_path / "Watch History.txt"
    p.write_text(
        "Date: 2026-05-12 08:14:03 UTC\r\n"
        "Link: https://www.tiktokv.com/share/video/1/\r\n"
        "\r\n"
        "Date: 2026-05-12 09:00:00 UTC\r\n"
        "Link: https://www.tiktokv.com/share/video/2/\r\n",
        encoding="utf-8",
    )
    videos, events = parse_export(p)
    assert len(events) == 2
    assert len(videos) == 2


def test_txt_handles_cr_only_line_endings(tmp_path: Path) -> None:
    """Old-Mac-style bare ``\\r`` separators DO parse — but only by accident.

    A naive read of the record regex (``Date:\\s*...\\r?\\n\\s*Link:...``)
    would suggest a bare ``\\r`` between Date and Link cannot match, since
    the ``\\n`` is required. In practice the file parses fine because
    ``Path.read_text`` opens the file in text mode and Python's universal
    newline handling silently translates bare ``\\r`` to ``\\n`` before the
    regex ever sees the bytes. By the time the regex runs, the input
    looks identical to an ``\\n``-separated file.

    This is fragile: if someone ever switched ``_parse_txt_export`` to
    operate on bytes (or opened the file with ``newline=''``), bare-CR
    files would suddenly produce zero events. This test pins the current
    end-to-end behaviour so that change would be caught loudly rather
    than silently degrading imports for some users.
    """
    p = tmp_path / "Watch History.txt"
    p.write_text(
        "Date: 2026-05-12 08:14:03 UTC\r"
        "Link: https://www.tiktokv.com/share/video/1/\r"
        "\r"
        "Date: 2026-05-12 09:00:00 UTC\r"
        "Link: https://www.tiktokv.com/share/video/2/\r",
        encoding="utf-8",
    )
    videos, events = parse_export(p)
    assert len(events) == 2
    assert len(videos) == 2


def test_txt_handles_mixed_lf_and_crlf(tmp_path: Path) -> None:
    """A file mixing ``\\n`` and ``\\r\\n`` across records still parses.

    Real exports occasionally arrive with mixed endings after being
    edited or round-tripped through a Windows tool. The regex tolerates
    this because the ``\\r`` is optional and inter-record whitespace is
    not constrained.
    """
    p = tmp_path / "Watch History.txt"
    p.write_text(
        "Date: 2026-05-12 08:14:03 UTC\n"
        "Link: https://www.tiktokv.com/share/video/1/\n"
        "\n"
        "Date: 2026-05-12 09:00:00 UTC\r\n"
        "Link: https://www.tiktokv.com/share/video/2/\r\n"
        "\r\n"
        "Date: 2026-05-12 10:00:00 UTC\n"
        "Link: https://www.tiktokv.com/share/video/3/\r\n",
        encoding="utf-8",
    )
    videos, events = parse_export(p)
    assert len(events) == 3
    assert len(videos) == 3


def test_txt_handles_missing_trailing_newline(tmp_path: Path) -> None:
    """A file whose last record has no terminating newline still parses.

    ``finditer`` happily picks up the final record because ``Link:`` is
    followed by ``\\S+`` (one-or-more non-whitespace) — no trailing
    newline is required after the URL.
    """
    p = tmp_path / "Watch History.txt"
    p.write_text(
        "Date: 2026-05-12 08:14:03 UTC\n"
        "Link: https://www.tiktokv.com/share/video/1/\n"
        "\n"
        "Date: 2026-05-12 09:00:00 UTC\n"
        "Link: https://www.tiktokv.com/share/video/2/",  # no trailing \n
        encoding="utf-8",
    )
    videos, events = parse_export(p)
    assert len(events) == 2
    assert len(videos) == 2


def test_txt_handles_date_with_timezone_offset(tmp_path: Path) -> None:
    """ISO-compatible offset like ``-05:00`` is preserved (not coerced to UTC).

    ``_parse_date`` first tries ``%Y-%m-%d %H:%M:%S`` and
    ``%Y-%m-%dT%H:%M:%S`` (both naive), which fail on an offset suffix,
    then falls back to ``datetime.fromisoformat``. fromisoformat
    accepts the space-separator plus a ``HH:MM``-style offset and
    yields a tz-aware datetime that we return as-is. The offset's UTC
    instant is what downstream callers care about.

    Note: on Python 3.14 ``fromisoformat`` is permissive enough to also
    accept ``-0500`` (no colon) and ``-05:00`` with a space separator,
    but the offset-without-minutes form (e.g. ``-5``) and named zones
    like ``EST`` still fail and the entry is dropped. This test pins
    only the colon-form path that the test is named after; that path
    is the one any reasonable export would use.
    """
    p = tmp_path / "Watch History.txt"
    p.write_text(
        "Date: 2026-05-17 12:00:00-05:00\n"
        "Link: https://www.tiktokv.com/share/video/1/\n",
        encoding="utf-8",
    )
    videos, events = parse_export(p)
    assert len(events) == 1
    assert len(videos) == 1

    watched = events[0].watched_at
    assert watched.tzinfo is not None
    assert watched.utcoffset() == timedelta(hours=-5)
    # The wall-clock 12:00:00 at -05:00 is 17:00:00 UTC.
    assert watched.astimezone(timezone.utc).hour == 17
    assert watched.astimezone(timezone.utc).minute == 0


def test_txt_handles_very_large_file_without_oom(tmp_path: Path) -> None:
    """10k records parse end-to-end without hanging or running out of memory.

    This is a soft performance/scale check: no profiling, just an
    assertion that the parser handles a realistic upper bound on a
    heavy TikTok user's history without choking. If it ever does, the
    test will either fail (count mismatch) or pytest will time out
    long before the suite finishes.
    """
    p = tmp_path / "Watch History.txt"
    n = 10_000
    lines: list[str] = []
    for i in range(n):
        # Vary the day and minute so dates are spread out and unique-ish.
        day = (i % 28) + 1
        minute = i % 60
        hour = (i // 60) % 24
        lines.append(f"Date: 2026-05-{day:02d} {hour:02d}:{minute:02d}:00 UTC")
        lines.append(f"Link: https://www.tiktokv.com/share/video/{i}/")
        lines.append("")
    p.write_text("\n".join(lines), encoding="utf-8")

    videos, events = parse_export(p)
    assert len(events) == n
    # Each Link URL is unique, so videos count equals events count.
    assert len(videos) == n
