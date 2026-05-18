"""FastAPI app.

Local-only API serving predicted average BOLD fMRI responses for a TikTok
watch history. See `docs/scientific-framing.md` — this app does NOT measure
the user's own brain.

Route contract: `shared/CONTRACTS.md` §7. Do not invent new routes here;
coordinate via the integration lead (terminal 1).
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

_log = logging.getLogger(__name__)

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from .aggregate import compute_aggregate
from .import_jobs import (
    CAPABILITY_BLOCKER_PRIORITY,
    CAP_MISSING_EXTRA,
    CAP_MISSING_FFMPEG,
    CAP_MISSING_GPU,
    CAP_MISSING_YT_DLP,
    ImportJob,
    ImportMode,
    ImportRunner,
    JobAlreadyRunning,
    JobNotFound,
    JobNotRetryable,
    default_orchestrator_factory,
    real_mode_available,
    real_mode_capabilities,
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


def _select_demo_store() -> Store | None:
    """Optional demo store, served when a request carries `?demo=1`.

    Reads from `<repo>/data/demo/sqlite/neural_media.db` by default;
    overridable via `NEURAL_MEDIA_DEMO_DB_PATH`. Returns None when no
    demo dataset is on disk — the dependency falls back to the live
    store in that case so the `?demo=1` toggle is a no-op rather than
    a 500.

    The demo dataset is a small, curated mock-mode ingest committed
    under `data/demo/` so a fresh checkout has something browsable
    without an import. See `docs/demo-dataset.md` for how it's baked.
    """
    db_path = os.environ.get("NEURAL_MEDIA_DEMO_DB_PATH")
    path = Path(db_path) if db_path else _repo_root() / "data" / "demo" / "sqlite" / "neural_media.db"
    if not path.is_file():
        return None
    return SqliteStore(path)


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
    # .txt covers the newer "Watch History.txt" export shape (flat
    # Date:/Link: blocks). The importer auto-detects format from the
    # extension and falls back to JSON for everything else.
    return lower.endswith((".json", ".zip", ".txt"))


# ---------------------------------------------------------------------------
# Structured error envelope
# ---------------------------------------------------------------------------

# Machine-readable error codes the frontend keys off of. Keep this list in
# sync with the capabilities blocker tokens (which are similar but live in
# a different vocabulary because the frontend renders different copy for
# "what's missing on this machine" vs. "why your POST was rejected").
ERR_FILE_EXTENSION_REJECTED = "file_extension_rejected"
ERR_SINCE_UNPARSEABLE = "since_unparseable"
ERR_REAL_EXTRA_MISSING = "real_extra_missing"
ERR_REAL_NO_FFMPEG = "real_no_ffmpeg"
ERR_REAL_NO_YT_DLP = "real_no_yt_dlp"
ERR_REAL_NO_GPU = "real_no_gpu"
ERR_RETRY_NOT_RETRYABLE = "retry_not_retryable"

# How each capability blocker token maps to the structured error_code.
_BLOCKER_TO_ERROR_CODE: dict[str, str] = {
    CAP_MISSING_EXTRA: ERR_REAL_EXTRA_MISSING,
    CAP_MISSING_FFMPEG: ERR_REAL_NO_FFMPEG,
    CAP_MISSING_YT_DLP: ERR_REAL_NO_YT_DLP,
    CAP_MISSING_GPU: ERR_REAL_NO_GPU,
}


def _error_response(
    *,
    status_code: int,
    error_code: str,
    detail: str,
) -> JSONResponse:
    """Standard error envelope.

    Body is ``{"error_code": "...", "detail": "..."}``. ``detail`` stays
    populated so existing clients that only read that field don't break.
    """
    return JSONResponse(
        status_code=status_code,
        content={"error_code": error_code, "detail": detail},
    )


def _pick_blocker_error_code(blockers: list[str]) -> str:
    """Return the highest-priority blocker's structured error_code.

    Falls back to ``ERR_REAL_EXTRA_MISSING`` if the priority list and the
    actual blockers somehow disagree — the user-visible string is still
    correct, just under a slightly broader code.
    """
    by_priority = {b: CAPABILITY_BLOCKER_PRIORITY.index(b)
                   if b in CAPABILITY_BLOCKER_PRIORITY else len(CAPABILITY_BLOCKER_PRIORITY)
                   for b in blockers}
    primary = min(blockers, key=lambda b: by_priority[b]) if blockers else CAP_MISSING_EXTRA
    return _BLOCKER_TO_ERROR_CODE.get(primary, ERR_REAL_EXTRA_MISSING)


def _blocker_detail(blockers: list[str]) -> str:
    """Human-readable detail string covering each present blocker."""
    msgs = []
    for b in blockers:
        if b == CAP_MISSING_EXTRA:
            msgs.append(
                "the [real] inference extra (pip install 'neural-media-inference[real]')"
            )
        elif b == CAP_MISSING_FFMPEG:
            msgs.append("ffmpeg on PATH")
        elif b == CAP_MISSING_YT_DLP:
            msgs.append("yt-dlp on PATH")
        elif b == CAP_MISSING_GPU:
            msgs.append("a CUDA-capable GPU (torch.cuda.is_available())")
    if not msgs:
        return "real mode is unavailable"
    return "real mode requires: " + "; ".join(msgs)


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
    app.state.demo_store = _select_demo_store()
    app.state.import_runner = import_runner if import_runner is not None else _build_default_import_runner()

    def pick_store(demo: bool = False) -> Store:
        """Per-request store selection.

        Every read endpoint takes this as a dependency. When the request
        carries `?demo=1` AND a demo dataset is on disk, serves from the
        demo store; otherwise the live store. Falls back silently to live
        when demo is requested but unavailable so a missing dataset is a
        graceful no-op, not a 500.
        """
        if demo and app.state.demo_store is not None:
            return app.state.demo_store
        return app.state.store

    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/videos", response_model=list[VideoMetadata])
    def list_videos(store: Store = Depends(pick_store)) -> list[VideoMetadata]:
        return store.list_videos()

    @app.get("/api/v1/videos/{video_id}", response_model=VideoMetadata)
    def get_video(video_id: str, store: Store = Depends(pick_store)) -> VideoMetadata:
        video = store.get_video(video_id)
        if video is None:
            raise HTTPException(status_code=404, detail="video not found")
        return video

    @app.get(
        "/api/v1/videos/{video_id}/metrics",
        response_model=list[RegionMetrics],
    )
    def get_video_metrics(
        video_id: str, store: Store = Depends(pick_store),
    ) -> list[RegionMetrics]:
        if store.get_video(video_id) is None:
            raise HTTPException(status_code=404, detail="video not found")
        return store.get_metrics(video_id)

    @app.get(
        "/api/v1/videos/{video_id}/activation",
        response_model=ActivationOutput,
    )
    def get_video_activation(
        video_id: str, store: Store = Depends(pick_store),
    ) -> ActivationOutput:
        if store.get_video(video_id) is None:
            raise HTTPException(status_code=404, detail="video not found")
        activation = store.get_activation(video_id)
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
    def get_aggregate(store: Store = Depends(pick_store)) -> AggregateReport:
        return compute_aggregate(store)

    @app.get("/api/v1/watch-events", response_model=list[WatchEvent])
    def list_watch_events(store: Store = Depends(pick_store)) -> list[WatchEvent]:
        return store.list_watch_events()

    @app.get("/api/v1/inference-runs", response_model=list[InferenceRun])
    def list_inference_runs(store: Store = Depends(pick_store)) -> list[InferenceRun]:
        return store.list_runs()

    @app.post("/api/v1/import", response_model=ImportJob)
    async def post_import(
        file: UploadFile = File(...),
        mode: Literal["mock", "real"] = Form("mock"),
        since: str | None = Form(None),
        until: str | None = Form(None),
        days: int | None = Form(None),
    ):
        filename = file.filename or "upload"
        if not _accepts_export_filename(filename):
            _log.info(
                "event=import_rejected reason=file_extension filename=%s",
                filename,
            )
            return _error_response(
                status_code=400,
                error_code=ERR_FILE_EXTENSION_REJECTED,
                detail="file must be a TikTok user_data.json, .zip, or .txt export",
            )
        if mode == "real":
            ok, blockers = real_mode_capabilities()
            if not ok:
                _log.info(
                    "event=import_rejected reason=real_mode_blocked "
                    "blockers=%s error_code=%s",
                    ",".join(blockers),
                    _pick_blocker_error_code(blockers),
                )
                return _error_response(
                    status_code=400,
                    error_code=_pick_blocker_error_code(blockers),
                    detail=_blocker_detail(blockers),
                )

        # Resolve the date window. ``days`` is the UI's preferred knob
        # ("last N days") and takes precedence over ``since`` when both
        # are supplied. ``until`` stays independent.
        try:
            since_dt = _resolve_since(days=days, since=since)
            until_dt = _parse_form_datetime(until) if until else None
        except ValueError as exc:
            return _error_response(
                status_code=400,
                error_code=ERR_SINCE_UNPARSEABLE,
                detail=str(exc),
            )

        runner: ImportRunner = app.state.import_runner
        imports_dir = _imports_dir()
        imports_dir.mkdir(parents=True, exist_ok=True)

        # Stream to a temp path inside data/imports/ so an aborted upload
        # never collides with the orchestrator's read. The job_id prefix
        # makes the file traceable to the row it'll spawn.
        import uuid as _uuid
        staged_id = str(_uuid.uuid4())
        safe_name = _safe_filename(filename)
        dest = imports_dir / f"{staged_id}__{safe_name}"
        with dest.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        await file.close()

        try:
            return runner.submit(
                dest, mode,
                source_filename=safe_name,
                since=since_dt,
                until=until_dt,
            )
        except JobAlreadyRunning as exc:
            # Clean up the staged upload — it'll never be read.
            try:
                dest.unlink()
            except OSError:
                pass
            # The 409 body MUST be the literal ImportJob shape (not a
            # `{"detail": ...}` envelope) — frontend reads it directly to
            # pick up the running job's id and resume polling.
            # See CONTRACTS.md §8.
            return JSONResponse(
                status_code=409,
                content=exc.running_job.model_dump(mode="json"),
            )

    @app.get("/api/v1/import/{job_id}", response_model=ImportJob)
    def get_import(job_id: str) -> ImportJob:
        runner: ImportRunner = app.state.import_runner
        job = runner.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="import job not found")
        return job

    @app.get("/api/v1/capabilities")
    def get_capabilities() -> dict:
        """Report which modes will actually work on this host.

        Frontend hits this on /import page mount so it can disable the
        "real" radio button (and explain why) before the user submits.
        ``mock`` is always true — the MockBackend ships with the base
        install and needs no external binaries.
        """
        ok, blockers = real_mode_capabilities()
        return {
            "mock": True,
            "real": ok,
            "real_blockers": blockers,
        }

    @app.get("/api/v1/debug")
    def get_debug() -> dict:
        """Host-side observability snapshot. See debug.py for shape."""
        from .debug import build_debug_payload
        runner: ImportRunner = app.state.import_runner
        return build_debug_payload(
            version=app.version,
            db_path=runner.db_path,
            videos_dir=runner.videos_dir,
            activations_dir=runner.activations_dir,
            imports_dir=_imports_dir(),
            jobs_store=runner.jobs,
        )

    @app.post("/api/v1/import/{job_id}/retry", response_model=ImportJob)
    def post_import_retry(job_id: str):
        """Re-drive a partially-failed batch without re-uploading the file.

        Calls ``orchestrator.run_pending`` on the same SQLite DB. Refuses
        unless the prior job ended in ``failed`` or ``partial``. Same
        singleton-gate semantics as POST /import — 409 with the running
        job's literal ImportJob shape when another job is in flight.
        """
        runner: ImportRunner = app.state.import_runner
        try:
            return runner.submit_retry(job_id)
        except JobNotFound:
            raise HTTPException(
                status_code=404, detail="import job not found"
            ) from None
        except JobNotRetryable as exc:
            return _error_response(
                status_code=409,
                error_code=ERR_RETRY_NOT_RETRYABLE,
                detail=str(exc),
            )
        except JobAlreadyRunning as exc:
            # Same shape as POST /import's 409: the literal ImportJob.
            return JSONResponse(
                status_code=409,
                content=exc.running_job.model_dump(mode="json"),
            )

    return app


def _safe_filename(name: str) -> str:
    """Strip directory components + keep the basename.

    Never trust the client's filename for path construction. We use it
    only as a human-readable suffix on the staged copy.
    """
    return Path(name).name or "upload"


def _parse_form_datetime(raw: str) -> datetime:
    """Parse a date or ISO-8601 datetime from a form field, stamping UTC.

    Raises ``ValueError`` (the caller turns this into 400) when the
    string is not a recognisable date/datetime.
    """
    raw = raw.strip()
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(
            f"not a valid date/datetime: {raw!r} (expected YYYY-MM-DD or ISO-8601)"
        ) from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _resolve_since(*, days: int | None, since: str | None) -> datetime | None:
    """Pick the effective ``since`` for the importer.

    ``days`` ("last N days") is the UI's preferred knob and wins over
    ``since`` when both are present. ``days <= 0`` disables the window.
    """
    if days is not None:
        if days <= 0:
            return None
        return datetime.now(timezone.utc) - timedelta(days=days)
    if since:
        return _parse_form_datetime(since)
    return None


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
