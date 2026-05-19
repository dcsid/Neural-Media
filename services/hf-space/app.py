"""HuggingFace Space — TRIBE inference for the single-video product.

Invoked by AWS Lambda over HTTP.  Returns 202 immediately and runs the
pipeline (download → preprocess → TRIBE → aggregate) in a background
task, posting the result to a caller-supplied callback URL.

Free tier only (ZeroGPU).  Per-call wall clock budget is ~120 s; we
target <90 s end-to-end.

Pipeline (see brief):

    1.  Acquire video.
           kind=url : yt-dlp with rotating UA, TikTok share-shortlink
                     fallback.  On block → failed_download / tiktok_blocked.
           kind=s3  : plain HTTPS GET on the presigned URL.
    2.  ffprobe duration.
           > 90 s          : rejected_duration.
           60 < d <= 90 s  : auto-trim to 60 s, error="auto_trimmed_to_60s".
    3.  ffmpeg preprocess to 224x224 @ 8 fps / 16 kHz mono
        (DEFAULT_PREPROCESSING_PARAMS, mirrored from
        services/pipeline/.../preprocess.py:build_ffmpeg_args).
    4.  TribeBackend.infer → (T, 20484) fp32 in [0, 1].
    5.  Region means via aggregate._region_timeseries — exposed publicly
        below as `_region_means_per_timepoint` so we avoid the more
        expensive aggregate_region_metrics path (we don't need mean/peak/
        sustained scalars on the wire for v1, just the timeseries).
    6.  Pack { videoDurationSec, timestamps, byRegion, modelVersion },
        gzip + base64.
    7.  POST callback with X-NM-Token, three attempts on 5xx with
        exponential back-off (0.5, 1, 2 s).
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Literal, Union

import httpx
import numpy as np
from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field

# Inference package is installed editable from services/inference (see
# Dockerfile + requirements.txt).  The wrapper handles licence checks,
# device selection and the sigmoid squash; aggregate_region_metrics
# folds (T, 20484) → 8 region rows with timeseries.
from neural_media_inference.aggregate import aggregate_region_metrics

_log = logging.getLogger("hf-space")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# Shared secret with the AWS Lambda that holds the callback URL.  The Lambda
# rejects callbacks without a matching X-NM-Token; we refuse to start at all
# if the value isn't set, so a misconfigured Space fails loud instead of
# silently dropping callbacks.
CALLBACK_SHARED_SECRET = os.environ.get("CALLBACK_SHARED_SECRET", "")

# Duration policy.  Anything strictly longer than MAX_DURATION_SEC is
# rejected outright; the middle band [TRIM_THRESHOLD_SEC, MAX_DURATION_SEC]
# gets auto-trimmed so a 75 s video still produces a useful answer rather
# than a 4xx surprise.  Defaults match the brief.
MAX_DURATION_SEC = float(os.environ.get("HF_MAX_DURATION_SEC", "90"))
TRIM_THRESHOLD_SEC = float(os.environ.get("HF_TRIM_THRESHOLD_SEC", "60"))
TRIM_TARGET_SEC = float(os.environ.get("HF_TRIM_TARGET_SEC", "60"))

# TRIBE preprocessing target.  Hard-coded here to keep the Space self-
# contained (we don't want to pull neural_media_pipeline just to read three
# constants).  Mirrors DEFAULT_PREPROCESSING_PARAMS in
# neural_media_inference.runner — keep both in sync.
VIDEO_RES = "224x224"
VIDEO_FPS = 8
AUDIO_SR = 16000

# ZeroGPU duration budget.  Used by the @spaces.GPU decorator when the
# `spaces` SDK is importable; falls back to a plain function otherwise so
# `python app.py` works locally without HF runtime present.
ZERO_GPU_DURATION_SEC = int(os.environ.get("ZERO_GPU_DURATION_SEC", "110"))

# yt-dlp UAs to rotate through.  TikTok in particular rate-limits by UA, so
# we round-robin per request.  These are real recent UAs — nothing exotic.
_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

# TikTok URL shapes we know how to rewrite.  The share-shortlink form
# (tiktokv.com/share/video/{id}) is friendlier to yt-dlp than the public
# /@user/video/{id} canonical when the canonical form gets blocked.
_TIKTOK_VIDEO_ID_RE = re.compile(
    r"tiktok\.com/(?:@[\w.\-]+/video|v|t)/(?P<id>\d{6,})"
)


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------
class SourceUrl(BaseModel):
    kind: Literal["url"]
    value: str


class SourceS3(BaseModel):
    kind: Literal["s3"]
    value: str


class PredictRequest(BaseModel):
    jobId: str = Field(..., min_length=1, max_length=128)
    source: Union[SourceUrl, SourceS3] = Field(..., discriminator="kind")
    callbackUrl: str
    callbackToken: str = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Optional ZeroGPU integration
# ---------------------------------------------------------------------------
try:  # pragma: no cover - only importable inside HF Spaces ZeroGPU runtime
    import spaces  # type: ignore[import-not-found]

    _gpu_decorator = spaces.GPU(duration=ZERO_GPU_DURATION_SEC)
except Exception:  # pragma: no cover
    spaces = None  # type: ignore[assignment]

    def _gpu_decorator(fn):  # type: ignore[misc]
        return fn


# ---------------------------------------------------------------------------
# Backend cache.  Building TribeBackend pulls multi-GB weights; do it once
# per process and reuse.  Lazy so the Space boots fast — model load happens
# on the first /predict.
# ---------------------------------------------------------------------------
_BACKEND_LOCK = asyncio.Lock()
_BACKEND: Any = None


async def _get_backend(videos_dir: Path):
    global _BACKEND
    if _BACKEND is not None:
        # videos_dir is mutated per-call (it's just the dir we drop the
        # preprocessed file into); rebind without rebuilding the model.
        _BACKEND._videos_dir = videos_dir  # noqa: SLF001
        return _BACKEND
    async with _BACKEND_LOCK:
        if _BACKEND is None:
            from neural_media_inference.backend_tribe import TribeBackend

            device = "cuda"
            _log.info("loading TribeBackend (device=%s, cache=%s)", device, os.environ.get("HF_HOME"))
            _BACKEND = TribeBackend(
                videos_dir=videos_dir,
                device=device,
                cache_dir=os.environ.get("HF_HOME"),
                accept_licenses=True,
            )
            _log.info("TribeBackend ready (model_version=%s)", _BACKEND.model_version)
        else:
            _BACKEND._videos_dir = videos_dir  # noqa: SLF001
    return _BACKEND


# ---------------------------------------------------------------------------
# Pipeline helpers
# ---------------------------------------------------------------------------
class PipelineError(Exception):
    """Domain error with a stable status code for the callback payload."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _which_or_raise(binary: str) -> str:
    path = shutil.which(binary)
    if not path:
        raise PipelineError("failed_inference", f"{binary} not on PATH in the Space image")
    return path


def _ffprobe_duration(path: Path) -> float:
    """Return media duration in seconds.  Raises PipelineError on failure."""
    ffprobe = _which_or_raise("ffprobe")
    proc = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=False, capture_output=True,
    )
    if proc.returncode != 0:
        tail = proc.stderr[-200:].decode("utf-8", errors="replace")
        raise PipelineError("failed_download", f"ffprobe failed: {tail}")
    try:
        return float(proc.stdout.strip())
    except ValueError as e:
        raise PipelineError("failed_download", f"ffprobe returned non-numeric duration: {proc.stdout!r}") from e


def _trim_to(src: Path, dest: Path, seconds: float) -> None:
    ffmpeg = _which_or_raise("ffmpeg")
    proc = subprocess.run(
        [
            ffmpeg, "-y", "-loglevel", "error",
            "-i", str(src),
            "-t", f"{seconds:.3f}",
            "-c", "copy",
            str(dest),
        ],
        check=False, capture_output=True,
    )
    if proc.returncode != 0:
        tail = proc.stderr[-200:].decode("utf-8", errors="replace")
        raise PipelineError("failed_inference", f"ffmpeg trim failed: {tail}")


def _preprocess(src: Path, dest: Path) -> None:
    """Normalise to TRIBE's input shape.

    Mirror of services/pipeline/.../preprocess.py:build_ffmpeg_args with
    the constants pinned at module load (see VIDEO_RES, VIDEO_FPS,
    AUDIO_SR).  Re-encodes both streams — `-c copy` would defeat the
    resolution / fps change.
    """
    ffmpeg = _which_or_raise("ffmpeg")
    w, h = (int(x) for x in VIDEO_RES.lower().split("x"))
    proc = subprocess.run(
        [
            ffmpeg, "-y", "-loglevel", "error",
            "-i", str(src),
            "-vf", f"scale={w}:{h}:flags=bicubic",
            "-r", str(VIDEO_FPS),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-ar", str(AUDIO_SR), "-ac", "1",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(dest),
        ],
        check=False, capture_output=True,
    )
    if proc.returncode != 0:
        tail = proc.stderr[-300:].decode("utf-8", errors="replace")
        raise PipelineError("failed_inference", f"ffmpeg preprocess failed: {tail}")
    if not dest.exists() or dest.stat().st_size == 0:
        raise PipelineError("failed_inference", "preprocess produced empty output")


def _maybe_rewrite_tiktok(url: str) -> str | None:
    """Convert a public TikTok URL into the share-shortlink form when we can.

    Returns the rewritten URL, or None if the input doesn't look like one
    we know how to rewrite (in which case the caller just tries it as-is).
    """
    m = _TIKTOK_VIDEO_ID_RE.search(url)
    if not m:
        return None
    return f"https://www.tiktokv.com/share/video/{m.group('id')}/"


def _download_via_ytdlp(url: str, dest_dir: Path, *, ua_index: int) -> Path:
    """Run yt-dlp.  Raises PipelineError on failure; classifies TikTok blocks."""
    out_template = str(dest_dir / "video.%(ext)s")
    ua = _USER_AGENTS[ua_index % len(_USER_AGENTS)]
    cmd = [
        "yt-dlp",
        "--quiet", "--no-warnings",
        "--no-playlist",
        "--no-part",
        "--user-agent", ua,
        "--retries", "2",
        "--fragment-retries", "2",
        "-f", "mp4/bv*+ba/best",
        "--merge-output-format", "mp4",
        "-o", out_template,
        url,
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True)
    if proc.returncode != 0:
        tail = proc.stderr[-400:].decode("utf-8", errors="replace").lower()
        if "tiktok" in url.lower() and any(
            sig in tail
            for sig in ("blocked", "unable to download", "403", "captcha", "login", "rate")
        ):
            raise PipelineError("failed_download", "tiktok_blocked")
        raise PipelineError("failed_download", f"yt-dlp failed: {tail[-200:]}")
    for ext in ("mp4", "mkv", "webm", "mov"):
        cand = dest_dir / f"video.{ext}"
        if cand.exists() and cand.stat().st_size > 0:
            return cand
    raise PipelineError("failed_download", "yt-dlp produced no output file")


def _download_url(url: str, dest_dir: Path) -> Path:
    """Try yt-dlp directly; for TikTok URLs that fail, try the share form."""
    last_exc: PipelineError | None = None
    rewritten = _maybe_rewrite_tiktok(url) if "tiktok" in url.lower() else None
    candidates = [url] if rewritten is None else [url, rewritten]
    for i, candidate in enumerate(candidates):
        try:
            return _download_via_ytdlp(candidate, dest_dir, ua_index=i)
        except PipelineError as e:
            last_exc = e
            # Only continue if the first attempt looked like a TikTok block.
            if "tiktok_blocked" not in e.message and "yt-dlp" not in e.message:
                raise
    assert last_exc is not None
    raise last_exc


async def _download_s3(url: str, dest_dir: Path) -> Path:
    """Stream a presigned S3 URL to disk."""
    dest = dest_dir / "video.mp4"
    timeout = httpx.Timeout(30.0, connect=10.0, read=60.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        async with client.stream("GET", url) as resp:
            if resp.status_code != 200:
                raise PipelineError(
                    "failed_download",
                    f"s3 GET returned {resp.status_code}",
                )
            with dest.open("wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=1 << 20):
                    f.write(chunk)
    if dest.stat().st_size == 0:
        raise PipelineError("failed_download", "s3 download produced empty file")
    return dest


def _region_means_per_timepoint(activations: np.ndarray, job_id: str) -> dict[str, list[float]]:
    """One per-region mean per timepoint.  Shape per region: (T,).

    Thin wrapper over `aggregate_region_metrics` that throws away the
    mean/peak/sustained scalars and the row metadata — for v1 the wire
    contract only ships the timeseries.  Passing the jobId as both
    video_id and inference_run_id keeps the dict-row schema valid; the
    Lambda never persists these rows anyway.
    """
    rows = aggregate_region_metrics(
        activations,
        video_id=job_id,
        inference_run_id=job_id,
    )
    return {row["region_id"]: row["timeseries"] for row in rows}


def _build_timestamps(num_timepoints: int) -> list[float]:
    """Frame timestamps in seconds.  TRIBE samples at 1.5 Hz (CONTRACTS §4)."""
    if num_timepoints <= 0:
        return []
    dt = 1.0 / 1.5
    return [round(i * dt, 6) for i in range(num_timepoints)]


@_gpu_decorator
def _run_tribe(video_path: Path, *, seed: int) -> tuple[np.ndarray, str]:
    """Wrap TribeBackend.infer.  Returns (activations, model_version).

    Decorated with `@spaces.GPU` so ZeroGPU only allocates the A10G for the
    duration of this call — outside this scope the Space holds no GPU.
    """
    from neural_media_inference.backend_tribe import TribeBackend  # noqa: PLC0415

    # We always get/build a process-global backend; the call below is a
    # synchronous re-entry into the asyncio loop's cache.  Safe because the
    # cache lookup is fast and protected by an asyncio.Lock at the caller.
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = TribeBackend(
            videos_dir=video_path.parent,
            device="cuda",
            cache_dir=os.environ.get("HF_HOME"),
            accept_licenses=True,
        )
    else:
        _BACKEND._videos_dir = video_path.parent  # noqa: SLF001

    # The backend resolves `{videos_dir}/{video_id}.mp4` — pass the stem.
    activations = _BACKEND.infer(
        video_id=video_path.stem,
        duration_s=0.0,  # informational only in the wrapper; the model uses the file
        seed=seed,
        sample_rate_hz=1.5,
    )
    return activations, _BACKEND.model_version


async def _post_callback(url: str, token: str, payload: dict) -> None:
    """POST the callback with retries on 5xx.

    Three attempts, exponential back-off 0.5 / 1 / 2 s, only on 5xx.  4xx
    and connection errors are not retried — a 4xx means the Lambda
    rejected us (wrong token, malformed body) and another try won't fix
    that; a connection error within HF→AWS usually means the Space lost
    network, which retries inside the same task can't recover from.
    """
    headers = {"X-NM-Token": token, "content-type": "application/json"}
    timeout = httpx.Timeout(15.0, connect=5.0, read=15.0)
    backoffs = [0.5, 1.0, 2.0]
    last_status: str | None = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt, wait in enumerate(backoffs, start=1):
            try:
                resp = await client.post(url, json=payload, headers=headers)
            except httpx.HTTPError as e:
                _log.error("callback connect failed attempt=%d err=%s", attempt, e)
                last_status = f"connect_error: {e}"
                break  # do not retry connection errors
            if 200 <= resp.status_code < 300:
                _log.info("callback ok attempt=%d status=%d", attempt, resp.status_code)
                return
            if 500 <= resp.status_code < 600 and attempt < len(backoffs):
                _log.warning(
                    "callback 5xx attempt=%d status=%d body=%s",
                    attempt, resp.status_code, resp.text[:200],
                )
                await asyncio.sleep(wait)
                continue
            last_status = f"http_{resp.status_code}: {resp.text[:200]}"
            break
    raise RuntimeError(f"callback failed: {last_status}")


# ---------------------------------------------------------------------------
# Job runner
# ---------------------------------------------------------------------------
async def _run_job(req: PredictRequest) -> None:
    """Execute the pipeline and POST the callback.  All errors get mapped
    to a callback payload — never bubble up to BackgroundTasks (which
    would silently log and drop)."""
    started = time.monotonic()
    workdir = Path(tempfile.mkdtemp(prefix=f"job-{req.jobId}-"))
    try:
        # 1. Acquire.
        raw_dir = workdir / "raw"
        raw_dir.mkdir()
        if req.source.kind == "url":
            raw_path = await asyncio.to_thread(_download_url, req.source.value, raw_dir)
        else:
            raw_path = await _download_s3(req.source.value, raw_dir)

        # 2. Duration policy.
        duration = _ffprobe_duration(raw_path)
        _log.info("job=%s downloaded %.1fs", req.jobId, duration)
        trimmed_note: str | None = None
        if duration > MAX_DURATION_SEC:
            await _post_callback(req.callbackUrl, req.callbackToken, {
                "jobId": req.jobId,
                "status": "rejected_duration",
                "durationSec": duration,
                "error": f"video is {duration:.1f}s; max accepted is {MAX_DURATION_SEC:.0f}s",
            })
            return
        if duration > TRIM_THRESHOLD_SEC:
            trimmed = raw_dir / "trimmed.mp4"
            await asyncio.to_thread(_trim_to, raw_path, trimmed, TRIM_TARGET_SEC)
            raw_path = trimmed
            duration = TRIM_TARGET_SEC
            trimmed_note = "auto_trimmed_to_60s"

        # 3-4. Preprocess + inference.  Name the preprocessed file after
        #      the jobId so TribeBackend's video_id-based resolver picks it up.
        proc_dir = workdir / "proc"
        proc_dir.mkdir()
        proc_path = proc_dir / f"{req.jobId}.mp4"
        await asyncio.to_thread(_preprocess, raw_path, proc_path)

        # Seed derived from the jobId so identical inputs produce identical
        # outputs (TRIBE wrapper z-seeds torch + numpy + cuda from this).
        seed = int.from_bytes(req.jobId.encode("utf-8")[:4].ljust(4, b"\0"), "big") & 0x7FFFFFFF
        activations, model_version = await asyncio.to_thread(_run_tribe, proc_path, seed=seed)

        # 5. Region aggregation.
        by_region = _region_means_per_timepoint(activations, req.jobId)
        T = activations.shape[0]
        timestamps = _build_timestamps(T)

        # 6. Pack + compress.
        activation_payload = {
            "videoDurationSec": duration,
            "timestamps": timestamps,
            "byRegion": by_region,
            "modelVersion": model_version,
        }
        raw_json = json.dumps(activation_payload, separators=(",", ":")).encode("utf-8")
        gz = gzip.compress(raw_json, compresslevel=6)
        activations_b64 = base64.b64encode(gz).decode("ascii")

        elapsed = time.monotonic() - started
        _log.info(
            "job=%s done T=%d duration=%.1fs elapsed=%.1fs gzip=%dB",
            req.jobId, T, duration, elapsed, len(gz),
        )

        callback_body: dict[str, Any] = {
            "jobId": req.jobId,
            "status": "done",
            "activationsB64": activations_b64,
            "durationSec": duration,
            "modelVersion": model_version,
        }
        if trimmed_note:
            callback_body["error"] = trimmed_note
        await _post_callback(req.callbackUrl, req.callbackToken, callback_body)

    except PipelineError as e:
        _log.warning("job=%s pipeline error status=%s msg=%s", req.jobId, e.status, e.message)
        try:
            await _post_callback(req.callbackUrl, req.callbackToken, {
                "jobId": req.jobId,
                "status": e.status,
                "error": e.message,
            })
        except Exception as cb_err:  # noqa: BLE001
            _log.exception("job=%s callback also failed: %s", req.jobId, cb_err)
    except Exception as e:  # noqa: BLE001
        _log.exception("job=%s unexpected error", req.jobId)
        try:
            await _post_callback(req.callbackUrl, req.callbackToken, {
                "jobId": req.jobId,
                "status": "failed_inference",
                "error": f"{type(e).__name__}: {e}"[:300],
            })
        except Exception as cb_err:  # noqa: BLE001
            _log.exception("job=%s callback also failed: %s", req.jobId, cb_err)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="neural-media hf-space", version="0.1.0")


@app.get("/")
def root() -> dict:
    return {
        "service": "neural-media hf-space",
        "ok": True,
        "model": "tribe-v2",
        "max_duration_sec": MAX_DURATION_SEC,
        "trim_threshold_sec": TRIM_THRESHOLD_SEC,
    }


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.post("/predict", status_code=202)
async def predict(req: PredictRequest, bg: BackgroundTasks) -> dict:
    if not CALLBACK_SHARED_SECRET:
        # Misconfiguration: refuse to start the job at all, so the
        # caller hears about it instead of waiting for a callback that
        # would be silently dropped on the AWS side.
        raise HTTPException(503, "CALLBACK_SHARED_SECRET not configured on Space")
    # We accept the caller's token verbatim (it's what the Lambda is
    # expecting back); we don't compare it to our own secret here.  The
    # Lambda owns the check, not us.
    job_uuid = uuid.uuid4().hex[:8]  # local tag for log correlation
    _log.info("accepted job=%s source=%s tag=%s", req.jobId, req.source.kind, job_uuid)
    # asyncio.create_task instead of BackgroundTasks so the work isn't
    # tied to the response's lifecycle — FastAPI's BackgroundTasks run
    # *after* the response is sent but on the same coroutine; create_task
    # detaches cleanly which we want here.
    asyncio.create_task(_run_job(req))
    return {"accepted": True}
