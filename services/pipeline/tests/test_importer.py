"""Smoke tests for the TikTok export importer.

These tests pin the fixture shape: if you change
``data/sample/tiktok_export/user_data.json``, expect to update them in the
same commit.
"""

from __future__ import annotations

import json
import logging
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from neural_media_pipeline.importer import (
    MalformedExportError,
    parse_export,
    stable_video_id,
    stable_watch_event_id,
)
from shared.schemas import VideoMetadata, WatchEvent

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = REPO_ROOT / "data" / "sample" / "tiktok_export" / "user_data.json"
TXT_FIXTURE = REPO_ROOT / "data" / "sample" / "tiktok_export" / "watch_history.txt"


def test_fixture_parses_to_eight_events_and_eight_videos() -> None:
    videos, events = parse_export(FIXTURE)
    # The fixture has 8 watch events across 8 distinct URLs.
    assert len(events) == 8
    assert len(videos) == 8
    assert {v.id for v in videos} == {e.video_id for e in events}


def test_round_trip_through_shared_schemas() -> None:
    videos, events = parse_export(FIXTURE)

    for v in videos:
        assert isinstance(v, VideoMetadata)
        VideoMetadata.model_validate(v.model_dump())
    for e in events:
        assert isinstance(e, WatchEvent)
        WatchEvent.model_validate(e.model_dump())


def test_video_fields_populated() -> None:
    videos, _ = parse_export(FIXTURE)
    by_author = {v.author: v for v in videos}

    # Authors are extracted from the @handle in the URL.
    assert {"sampleuser", "chefcorpus", "traveltoks", "late.nite",
            "asmrsoft", "newsdaily", "comedybit", "morningbrew"} == set(by_author)

    chef = by_author["chefcorpus"]
    assert chef.downloaded is False
    assert chef.local_path is None
    assert chef.duration_s == 0.0
    assert chef.tags == []
    assert chef.title is None
    assert chef.source_url.startswith("https://www.tiktok.com/")


def test_watched_at_is_utc_aware() -> None:
    _, events = parse_export(FIXTURE)
    for e in events:
        assert e.watched_at.tzinfo is not None
        assert e.watched_at.utcoffset() == timezone.utc.utcoffset(e.watched_at)
    # Fixture spans 2026-05-12 .. 2026-05-14.
    earliest = min(e.watched_at for e in events)
    latest = max(e.watched_at for e in events)
    assert earliest.date().isoformat() == "2026-05-12"
    assert latest.date().isoformat() == "2026-05-14"


def test_stable_video_id_is_deterministic() -> None:
    url = "https://www.tiktok.com/@chefcorpus/video/7300000000000000002"
    assert stable_video_id(url) == stable_video_id(url)
    # Different URL → different id.
    other = "https://www.tiktok.com/@chefcorpus/video/7300000000000000003"
    assert stable_video_id(url) != stable_video_id(other)


def test_parse_export_is_idempotent() -> None:
    videos_a, events_a = parse_export(FIXTURE)
    videos_b, events_b = parse_export(FIXTURE)
    assert [v.id for v in videos_a] == [v.id for v in videos_b]
    assert [e.id for e in events_a] == [e.id for e in events_b]


def test_watch_event_id_distinguishes_timestamps() -> None:
    videos, events = parse_export(FIXTURE)
    # All 8 fixture events have distinct (video_id, watched_at) — so all ids unique.
    assert len({e.id for e in events}) == 8

    # Same video, different timestamps → different ids; same → same.
    v = videos[0]
    e = events[0]
    assert stable_watch_event_id(v.id, e.watched_at) == e.id


def test_tolerates_missing_and_malformed_entries(tmp_path: Path) -> None:
    payload = {
        "Activity": {
            "Video Browsing History": {
                "VideoList": [
                    {"Date": "2026-05-12 08:14:03",
                     "Link": "https://www.tiktok.com/@a/video/1"},
                    {"Date": "2026-05-12 08:14:42"},                    # no Link
                    {"Link": "https://www.tiktok.com/@b/video/2"},      # no Date
                    {"Date": "not a date",
                     "Link": "https://www.tiktok.com/@c/video/3"},      # bad Date
                    "not even a dict",                                  # garbage
                    {"Date": "2026-05-12 09:00:00",
                     "Link": "https://www.tiktok.com/@d/video/4"},
                ]
            }
        }
    }
    p = tmp_path / "user_data.json"
    p.write_text(json.dumps(payload), encoding="utf-8")

    videos, events = parse_export(p)
    assert len(events) == 2
    assert len(videos) == 2


def test_duplicate_urls_dedupe_videos_but_keep_events(tmp_path: Path) -> None:
    url = "https://www.tiktok.com/@dup/video/1"
    payload = {
        "Activity": {
            "Video Browsing History": {
                "VideoList": [
                    {"Date": "2026-05-12 08:14:03", "Link": url},
                    {"Date": "2026-05-12 09:00:00", "Link": url},
                    {"Date": "2026-05-12 10:00:00", "Link": url},
                ]
            }
        }
    }
    p = tmp_path / "user_data.json"
    p.write_text(json.dumps(payload), encoding="utf-8")

    videos, events = parse_export(p)
    assert len(videos) == 1
    assert len(events) == 3
    assert all(e.video_id == videos[0].id for e in events)


def test_empty_or_missing_browsing_history(tmp_path: Path) -> None:
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({"Profile": {}}), encoding="utf-8")
    videos, events = parse_export(p)
    assert videos == []
    assert events == []


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        parse_export(tmp_path / "does_not_exist.json")


# ---------------------------------------------------------------------------
# Corrupt / undecodable payloads — tolerant of per-entry drift, but a
# wholesale-undecodable file raises a clear, typed MalformedExportError
# (not a raw json/UnicodeDecodeError) so the api can surface a `failed`
# import job per CONTRACTS.md §8.
# ---------------------------------------------------------------------------

def test_zip_with_truncated_json_raises_malformed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    p = tmp_path / "export.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("user_data.json", '{"Activity": {"Video Browsing Hist')  # truncated

    caplog.set_level(logging.DEBUG, logger="neural_media_pipeline.importer")
    with pytest.raises(MalformedExportError):
        parse_export(p)
    assert any("event=export_parse_failed" in r.getMessage() for r in caplog.records)


def test_zip_with_invalid_utf8_raises_malformed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    p = tmp_path / "export.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("user_data.json", b"\xff\xfe\x00 not valid utf-8 \xff")  # bad bytes

    caplog.set_level(logging.DEBUG, logger="neural_media_pipeline.importer")
    with pytest.raises(MalformedExportError):
        parse_export(p)
    assert any("event=export_parse_failed" in r.getMessage() for r in caplog.records)


def test_plain_truncated_json_raises_malformed(tmp_path: Path) -> None:
    p = tmp_path / "user_data.json"
    p.write_text('{"Activity": {', encoding="utf-8")  # truncated, not a zip
    with pytest.raises(MalformedExportError):
        parse_export(p)


def test_malformed_export_error_is_value_error() -> None:
    """Subclassing ValueError keeps it catchable by callers that already
    treat bad input as a ValueError."""
    assert issubclass(MalformedExportError, ValueError)


# ---------------------------------------------------------------------------
# Newer .txt export format ("Watch History.txt")
# ---------------------------------------------------------------------------


def test_txt_fixture_parses_to_eight_events_and_eight_videos() -> None:
    videos, events = parse_export(TXT_FIXTURE)
    assert len(events) == 8
    assert len(videos) == 8


def test_txt_export_strips_utc_suffix_and_assumes_utc() -> None:
    _, events = parse_export(TXT_FIXTURE)
    for e in events:
        assert e.watched_at.tzinfo is not None
        assert e.watched_at.utcoffset() == timezone.utc.utcoffset(e.watched_at)


def test_txt_export_leaves_author_none_for_share_shortlinks() -> None:
    # tiktokv.com/share/video/<id>/ has no @handle, so author is unset.
    videos, _ = parse_export(TXT_FIXTURE)
    assert all(v.author is None for v in videos)
    assert all("tiktokv.com/share/video/" in v.source_url for v in videos)


def test_txt_export_tolerates_extra_blank_lines_and_partial_records(tmp_path: Path) -> None:
    p = tmp_path / "Watch History.txt"
    p.write_text(
        "Date: 2026-05-12 08:14:03 UTC\n"
        "Link: https://www.tiktokv.com/share/video/1/\n"
        "\n"
        "\n"  # extra blank line
        "Date: not a date\n"
        "Link: https://www.tiktokv.com/share/video/2/\n"
        "\n"
        "Date: 2026-05-12 09:00:00 UTC\n"
        # missing Link line — record should be silently dropped
        "\n"
        "Date: 2026-05-12 10:00:00 UTC\n"
        "Link: https://www.tiktokv.com/share/video/3/\n",
        encoding="utf-8",
    )
    videos, events = parse_export(p)
    # Records 1 and 3 are valid; record 2 has an unparseable date, record
    # with no Link is missing the required field.
    assert len(events) == 2
    assert len(videos) == 2


# ---------------------------------------------------------------------------
# Date-range filter
# ---------------------------------------------------------------------------


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def test_since_filter_includes_only_in_window_events() -> None:
    # Fixture spans 2026-05-12 .. 2026-05-14. since=2026-05-13 drops
    # everything before midnight 2026-05-13.
    _, events = parse_export(FIXTURE, since=_utc("2026-05-13T00:00:00"))
    assert {e.watched_at.date().isoformat() for e in events} == {
        "2026-05-13", "2026-05-14",
    }


def test_until_filter_is_exclusive_upper_bound() -> None:
    # until=2026-05-13 keeps 2026-05-12 events only.
    _, events = parse_export(FIXTURE, until=_utc("2026-05-13T00:00:00"))
    assert {e.watched_at.date().isoformat() for e in events} == {"2026-05-12"}


def test_date_window_drops_video_with_no_in_window_events() -> None:
    # Window contains only the 2026-05-14 morning event; we should still
    # surface its video and only its video.
    videos, events = parse_export(
        FIXTURE,
        since=_utc("2026-05-14T00:00:00"),
        until=_utc("2026-05-15T00:00:00"),
    )
    assert len(events) == 1
    assert len(videos) == 1
    assert events[0].video_id == videos[0].id
