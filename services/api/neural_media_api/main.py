"""FastAPI app.

Local-only API serving predicted average BOLD fMRI responses for a TikTok
watch history. See `docs/scientific-framing.md` — this app does NOT measure
the user's own brain.

Route contract: `shared/CONTRACTS.md` §7. Do not invent new routes here;
coordinate via the integration lead (terminal 1).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from .aggregate import compute_aggregate
from .import_jobs import (
    ImportJob,
    ImportMode,
    ImportRunner,
    JobAlreadyRunning,
    default_orchestrator_factory,
    real_mode_available,
)
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
from .sqlite_store import SqliteStore, init_db
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


def _repo_root() -> Path:
    # services/api/neural_media_api/main.py -> repo root is 3 levels up.
    return Path(__file__).resolve().parents[3]


def _default_sample_root() -> Path:
    """Sample mock_inference directory. `NEURAL_MEDIA_SAMPLE_ROOT` overrides."""
    env = os.environ.get("NEURAL_MEDIA_SAMPLE_ROOT")
    return Path(env) if env else _repo_root() / "data" / "sample" / "mock_inference"


def _default_export_path() -> Path:
    """Committed TikTok export. `NEURAL_MEDIA_TIKTOK_EXPORT` overrides."""
    env = os.environ.get("NEURAL_MEDIA_TIKTOK_EXPORT")
    return (
        Path(env)
        if env
        else _repo_root() / "data" / "sample" / "tiktok_export" / "user_data.json"
    )


def _select_store() -> Store:
    """Pick the backing store based on environment.

    `NEURAL_MEDIA_DB_PATH` → SqliteStore. Otherwise → SampleStore reading
    the committed fixtures. Either way, missing files degrade to empty
    lists rather than crashing the API.
    """
    db_path = os.environ.get("NEURAL_MEDIA_DB_PATH")
    if db_path:
        return SqliteStore(db_path)
    return SampleStore(
        mock_inference_root=_default_sample_root(),
        tiktok_export_path=_default_export_path(),
    )


def _import_db_path() -> Path:
    """SQLite DB the import flow targets.

    Imports always write to the canonical catalog DB so subsequent reads
    via SqliteStore (started with the same NEURAL_MEDIA_DB_PATH) see the
    new rows. If the API itself is currently bound to SampleStore, the
    import still succeeds — the user just won't see results in the
    catalog endpoints until the API is restarted with the env var set.
    """
    env = os.environ.get("NEURAL_MEDIA_DB_PATH")
    return Path(env) if env else _repo_root() / "data" / "sqlite" / "neural_media.db"


def _imports_dir() -> Path:
    env = os.environ.get("NEURAL_MEDIA_IMPORTS_DIR")
    return Path(env) if env else _repo_root() / "data" / "imports"


def _videos_dir() -> Path:
    return _repo_root() / "data" / "videos"


def _processed_dir() -> Path:
    return _repo_root() / "data" / "processed"


def _activations_dir() -> Path:
    return _repo_root() / "data" / "activations"


def _accepts_export_filename(name: str) -> bool:
    lower = name.lower()
    return lower.endswith(".json") or lower.endswith(".zip")


def _build_default_import_runner() -> ImportRunner:
    """Wire the real Orchestrator factory against repo-root data dirs.

    Ensures the catalog DB exists (so the import_jobs table is provisioned
    before the first POST). The orchestrator itself will also call its own
    schema init; both are idempotent.
    """
    db_path = _import_db_path()
    init_db(db_path)
    return ImportRunner(
        db_path=db_path,
        activations_dir=_activations_dir(),
        videos_dir=_videos_dir(),
        processed_dir=_processed_dir(),
        orchestrator_factory=default_orchestrator_factory,
    )


def create_app(
    store: Store | None = None,
    *,
    import_runner: ImportRunner | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Neural Media API",
        version="0.2.0",
        description=(
            "Local-only API. Serves the predicted average BOLD fMRI response "
            "(TRIBE v2, averaged over 720 training subjects) for a user's "
            "TikTok watch history. This is NOT a measurement of the user's "
            "own brain — see docs/scientific-framing.md."
        ),
    )

    app.add_middleware(LoopbackOnlyMiddleware)
    # POST is required for /api/v1/import; every other route is GET, so the
    # cross-origin door we open here is consumed only by that one endpoint.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    app.state.store = store if store is not None else _select_store()
    app.state.import_runner = import_runner if import_runner is not None else _build_default_import_runner()

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

    @app.post("/api/v1/import", response_model=ImportJob, status_code=202)
    async def post_import(
        file: UploadFile = File(...),
        mode: Literal["mock", "real"] = Form("mock"),
    ) -> ImportJob:
        filename = file.filename or "upload"
        if not _accepts_export_filename(filename):
            raise HTTPException(
                status_code=400,
                detail="file must be a TikTok user_data.json or a .zip export",
            )
        if mode == "real" and not real_mode_available():
            raise HTTPException(
                status_code=400,
                detail=(
                    "real mode requires the [real] inference extra: "
                    "pip install 'neural-media-inference[real]'"
                ),
            )

        runner: ImportRunner = app.state.import_runner
        imports_dir = _imports_dir()
        imports_dir.mkdir(parents=True, exist_ok=True)

        # Stream to a temp path inside data/imports/ so an aborted upload
        # never collides with the orchestrator's read. The job_id prefix
        # makes the file traceable to the row it'll spawn.
        import uuid as _uuid
        staged_id = str(_uuid.uuid4())
        dest = imports_dir / f"{staged_id}__{_safe_filename(filename)}"
        with dest.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        await file.close()

        try:
            return runner.submit(dest, mode)
        except JobAlreadyRunning as exc:
            # Clean up the staged upload — it'll never be read.
            try:
                dest.unlink()
            except OSError:
                pass
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "another import is in progress",
                    "running_job_id": exc.running_job.id,
                },
            ) from exc

    @app.get("/api/v1/import/{job_id}", response_model=ImportJob)
    def get_import(job_id: str) -> ImportJob:
        runner: ImportRunner = app.state.import_runner
        job = runner.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="import job not found")
        return job

    return app


def _safe_filename(name: str) -> str:
    """Strip directory components + keep the basename.

    Never trust the client's filename for path construction. We use it
    only as a human-readable suffix on the staged copy.
    """
    return Path(name).name or "upload"


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
