"""Contract endpoints — round-trip every GET through shared.schemas.

Both store implementations (SampleStore + SqliteStore) are exercised
behind the same endpoints via the parametrized `client` fixture, so the
same contract assertions cover both paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from neural_media_api.main import create_app
from neural_media_api.sqlite_store import SqliteStore
from neural_media_api.store import SampleStore
from shared.schemas import (
    REGION_IDS,
    ActivationOutput,
    AggregateReport,
    InferenceRun,
    RegionDef,
    RegionMetrics,
    VideoMetadata,
    WatchEvent,
)


@pytest.fixture(params=["sample", "sqlite"])
def client(
    request: pytest.FixtureRequest,
    sample_root: Path,
    tiktok_export_path: Path,
    populated_sqlite_db: Path,
) -> Iterator[TestClient]:
    if request.param == "sample":
        store = SampleStore(sample_root, tiktok_export_path=tiktok_export_path)
    else:
        store = SqliteStore(populated_sqlite_db)
    app = create_app(store=store)
    # base_url controls the synthetic Host header — must be loopback to
    # satisfy LoopbackOnlyMiddleware.
    with TestClient(app, base_url="http://localhost") as tc:
        yield tc


@pytest.fixture
def empty_client(empty_sample_root: Path) -> TestClient:
    app = create_app(store=SampleStore(empty_sample_root))
    return TestClient(app, base_url="http://localhost")


def test_health(client: TestClient) -> None:
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_videos_round_trip(
    client: TestClient, populated_video_ids: list[str]
) -> None:
    r = client.get("/api/v1/videos")
    assert r.status_code == 200
    videos = [VideoMetadata.model_validate(v) for v in r.json()]
    assert {v.id for v in videos} == set(populated_video_ids)


def test_video_detail_round_trip(
    client: TestClient, populated_video_ids: list[str]
) -> None:
    r = client.get(f"/api/v1/videos/{populated_video_ids[0]}")
    assert r.status_code == 200
    VideoMetadata.model_validate(r.json())


def test_unknown_video_is_404(client: TestClient) -> None:
    assert client.get("/api/v1/videos/nope").status_code == 404
    assert client.get("/api/v1/videos/nope/metrics").status_code == 404
    assert client.get("/api/v1/videos/nope/activation").status_code == 404


def test_video_metrics_round_trip(
    client: TestClient, populated_video_ids: list[str]
) -> None:
    r = client.get(f"/api/v1/videos/{populated_video_ids[0]}/metrics")
    assert r.status_code == 200
    rows = [RegionMetrics.model_validate(m) for m in r.json()]
    assert {row.region_id for row in rows} == set(REGION_IDS)


def test_video_activation_round_trip(
    client: TestClient, populated_video_ids: list[str]
) -> None:
    r = client.get(f"/api/v1/videos/{populated_video_ids[0]}/activation")
    assert r.status_code == 200
    payload = ActivationOutput.model_validate(r.json())
    # Wire format must include every canonical region.
    assert set(payload.region_means) == set(REGION_IDS)


def test_regions_round_trip(client: TestClient) -> None:
    r = client.get("/api/v1/regions")
    assert r.status_code == 200
    rows = [RegionDef.model_validate(x) for x in r.json()]
    assert tuple(row.region_id for row in rows) == REGION_IDS


def test_aggregate_round_trip(client: TestClient) -> None:
    r = client.get("/api/v1/aggregate")
    assert r.status_code == 200
    report = AggregateReport.model_validate(r.json())
    assert report.total_videos == 2
    assert len(report.by_hour_of_day) == 24
    assert len(report.by_day_of_week) == 7
    assert set(report.by_region) == set(REGION_IDS)
    assert report.first_watched_at is not None
    assert report.last_watched_at is not None


def test_watch_events_round_trip(client: TestClient) -> None:
    r = client.get("/api/v1/watch-events")
    assert r.status_code == 200
    events = [WatchEvent.model_validate(e) for e in r.json()]
    assert len(events) == 3


def test_inference_runs_round_trip(
    client: TestClient, populated_video_ids: list[str]
) -> None:
    r = client.get("/api/v1/inference-runs")
    assert r.status_code == 200
    runs = [InferenceRun.model_validate(x) for x in r.json()]
    assert {run.video_id for run in runs} == set(populated_video_ids)


# --- Empty-fixture path: every list endpoint serves 200 + [] (not 500) ----


def test_empty_fixture_list_endpoints(empty_client: TestClient) -> None:
    for path in (
        "/api/v1/videos",
        "/api/v1/watch-events",
        "/api/v1/inference-runs",
    ):
        r = empty_client.get(path)
        assert r.status_code == 200, path
        assert r.json() == [], path


def test_empty_fixture_aggregate_is_zeroed(empty_client: TestClient) -> None:
    r = empty_client.get("/api/v1/aggregate")
    assert r.status_code == 200
    report = AggregateReport.model_validate(r.json())
    assert report.total_videos == 0
    assert report.total_watch_time_s == 0.0
    assert report.first_watched_at is None
    assert report.last_watched_at is None
    assert report.by_region == {}
    assert report.by_hour_of_day == [0.0] * 24
    assert report.by_day_of_week == [0.0] * 7
    assert report.clusters == []


def test_empty_fixture_regions_still_works(empty_client: TestClient) -> None:
    # /regions does not depend on the store — it is the canonical region set.
    r = empty_client.get("/api/v1/regions")
    assert r.status_code == 200
    assert len(r.json()) == len(REGION_IDS)
