"""SampleStore round-trip + missing-fixture behaviour."""

from __future__ import annotations

from pathlib import Path

from neural_media_api.store import SampleStore


def test_sample_store_populates_from_fixture(
    sample_root: Path,
    tiktok_export_path: Path,
    populated_video_ids: list[str],
    populated_regions: tuple[str, ...],
) -> None:
    store = SampleStore(sample_root, tiktok_export_path=tiktok_export_path)

    videos = store.list_videos()
    assert {v.id for v in videos} == set(populated_video_ids)

    metrics = store.get_metrics(populated_video_ids[0])
    assert {m.region_id for m in metrics} == set(populated_regions)

    activation = store.get_activation(populated_video_ids[0])
    assert activation is not None
    assert activation.video_id == populated_video_ids[0]
    assert set(activation.region_means) == set(populated_regions)

    # Three watch events: two on vid[0], one on vid[1].
    assert len(store.list_watch_events()) == 3
    assert len(store.list_runs()) == len(populated_video_ids)


def test_sample_store_without_export_still_serves_videos(
    sample_root: Path, populated_video_ids: list[str]
) -> None:
    """No TikTok export means no watch events, but videos/metrics still load."""
    store = SampleStore(sample_root, tiktok_export_path=None)
    assert {v.id for v in store.list_videos()} == set(populated_video_ids)
    assert store.list_watch_events() == []


def test_sample_store_export_only_has_videos_no_runs(
    tiktok_export_path: Path, tmp_path: Path
) -> None:
    """If only the export is present (no inference outputs), videos still appear."""
    empty_mock = tmp_path / "empty_mock"
    empty_mock.mkdir()
    store = SampleStore(empty_mock, tiktok_export_path=tiktok_export_path)
    assert len(store.list_videos()) == 2
    assert len(store.list_watch_events()) == 3
    assert store.list_runs() == []
    assert store.get_metrics(store.list_videos()[0].id) == []
    assert store.get_activation(store.list_videos()[0].id) is None


def test_missing_fixture_directory_yields_empty(tmp_path: Path) -> None:
    store = SampleStore(tmp_path / "does-not-exist")
    assert store.list_videos() == []
    assert store.list_watch_events() == []
    assert store.list_runs() == []
    assert store.get_metrics("anything") == []
    assert store.get_activation("anything") is None
    assert store.get_video("anything") is None


def test_empty_but_existing_fixture_directory_yields_empty(
    empty_sample_root: Path,
) -> None:
    store = SampleStore(empty_sample_root)
    assert store.list_videos() == []
    assert store.list_watch_events() == []
    assert store.list_runs() == []


def test_version_changes_when_fixture_changes(
    sample_root: Path, tmp_path: Path
) -> None:
    """A regenerated index.json must bump the version marker."""
    store_before = SampleStore(sample_root)
    v1 = store_before.version()

    # Touch index.json with a different mtime + content.
    index = sample_root / "index.json"
    index.write_text(index.read_text() + " ")

    store_after = SampleStore(sample_root)
    v2 = store_after.version()
    assert v1 != v2
