"""FastAPI app.

Local-only API serving predicted average BOLD fMRI responses for a TikTok
watch history. See `docs/scientific-framing.md` — this app does NOT measure
the user's own brain.

Route contract: `shared/CONTRACTS.md` §7. Do not invent new routes here;
coordinate via the integration lead (terminal 1).
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from .aggregate import compute_aggregate
from .schemas_re_export import (
    REGION_DESCRIPTIONS,
    REGION_IDS,
    ActivationOutput,
    AggregateReport,
    InferenceRun,
    RegionDef,
    RegionMetrics,
    VideoMetadata,
    WatchEvent,
)
from .store import SampleStore, Store

# Loopback-only hosts permitted on the `Host` header. `Host` may include a
# port (e.g. `localhost:8000`) — we strip it before matching.
_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1"})


class LoopbackOnlyMiddleware(BaseHTTPMiddleware):
    """Reject requests whose `Host` header is not a loopback name.

    The app also binds to 127.0.0.1 at the socket layer (see Makefile
    `dev-api` target / `if __name__ == "__main__"` block below), so this is
    defense-in-depth against DNS rebinding from a browser on the same host.
    """

    async def dispatch(self, request: Request, call_next):
        host_header = request.headers.get("host", "")
        host = host_header.split(":", 1)[0].strip().lower()
        if host and host not in _ALLOWED_HOSTS:
            return Response(
                content='{"detail":"Host header not permitted"}',
                status_code=400,
                media_type="application/json",
            )
        return await call_next(request)


def _default_sample_root() -> Path:
    """Resolve the sample mock_inference directory.

    Overridable via the `NEURAL_MEDIA_SAMPLE_ROOT` env var so tests can point
    at a fixture directory without touching the repo's committed sample data.
    """
    env = os.environ.get("NEURAL_MEDIA_SAMPLE_ROOT")
    if env:
        return Path(env)
    # services/api/neural_media_api/main.py -> repo root is 3 levels up.
    return Path(__file__).resolve().parents[3] / "data" / "sample" / "mock_inference"


def create_app(store: Store | None = None) -> FastAPI:
    app = FastAPI(
        title="Neural Media API",
        version="0.1.0",
        description=(
            "Local-only API. Serves the predicted average BOLD fMRI response "
            "(TRIBE v2, averaged over 720 training subjects) for a user's "
            "TikTok watch history. This is NOT a measurement of the user's "
            "own brain — see docs/scientific-framing.md."
        ),
    )

    app.add_middleware(LoopbackOnlyMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    backing: Store = store if store is not None else SampleStore(_default_sample_root())
    app.state.store = backing

    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/videos", response_model=list[VideoMetadata])
    def list_videos() -> list[VideoMetadata]:
        return app.state.store.list_videos()

    @app.get("/api/v1/videos/{video_id}", response_model=VideoMetadata)
    def get_video(video_id: str) -> VideoMetadata:
        video = app.state.store.get_video(video_id)
        if video is None:
            raise HTTPException(status_code=404, detail="video not found")
        return video

    @app.get(
        "/api/v1/videos/{video_id}/metrics",
        response_model=list[RegionMetrics],
    )
    def get_video_metrics(video_id: str) -> list[RegionMetrics]:
        if app.state.store.get_video(video_id) is None:
            raise HTTPException(status_code=404, detail="video not found")
        return app.state.store.get_metrics(video_id)

    @app.get(
        "/api/v1/videos/{video_id}/activation",
        response_model=ActivationOutput,
    )
    def get_video_activation(video_id: str) -> ActivationOutput:
        if app.state.store.get_video(video_id) is None:
            raise HTTPException(status_code=404, detail="video not found")
        activation = app.state.store.get_activation(video_id)
        if activation is None:
            raise HTTPException(status_code=404, detail="activation not found")
        return activation

    @app.get("/api/v1/regions", response_model=list[RegionDef])
    def list_regions() -> list[RegionDef]:
        return [
            RegionDef(region_id=rid, description=REGION_DESCRIPTIONS[rid])
            for rid in REGION_IDS
        ]

    @app.get("/api/v1/aggregate", response_model=AggregateReport)
    def get_aggregate() -> AggregateReport:
        return compute_aggregate(app.state.store)

    @app.get("/api/v1/watch-events", response_model=list[WatchEvent])
    def list_watch_events() -> list[WatchEvent]:
        return app.state.store.list_watch_events()

    @app.get("/api/v1/inference-runs", response_model=list[InferenceRun])
    def list_inference_runs() -> list[InferenceRun]:
        return app.state.store.list_runs()

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    # Loopback binding is non-negotiable — see docs/architecture.md.
    uvicorn.run(
        "neural_media_api.main:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
    )
