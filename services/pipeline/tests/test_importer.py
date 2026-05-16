"""Smoke tests for the TikTok export importer.

These tests pin the fixture shape: if you change
``data/sample/tiktok_export/user_data.json``, expect to update them in the
same commit.
"""

from __future__ import annotations

import json
from datetime import timezone
from pathlib import Path

import pytest

from neural_media_pipeline.importer import (
    parse_export,
    stable_video_id,
    stable_watch_event_id,
)
from shared.schemas import VideoMetadata, WatchEvent

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = REPO_ROOT / "data" / "sample" / "tiktok_export" / "user_data.json"


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
