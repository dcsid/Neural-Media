"""SampleStore round-trip + missing-fixture behaviour."""

from __future__ import annotations

from pathlib import Path

from neural_media_api.store import SampleStore


def test_sample_store_populates_from_fixture(
    sample_root: Path,
    populated_video_ids: list[str],
    populated_regions: tuple[str, ...],
) -> None:
    store = SampleStore(sample_root)

    videos = store.list_videos()
    assert [v.id for v in videos] == populated_video_ids

    metrics = store.get_metrics(populated_video_ids[0])
    assert {m.region_id for m in metrics} == set(populated_regions)

    activation = store.get_activation(populated_video_ids[0])
    assert activation is not None
    assert activation.video_id == populated_video_ids[0]
    assert set(activation.region_means) == set(populated_regions)

    assert len(store.list_watch_events()) == 3
    assert len(store.list_runs()) == len(populated_video_ids)


def test_missing_fixture_directory_yields_empty(tmp_path: Path) -> None:
    # A path that does not exist.
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
