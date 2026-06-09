# HuggingFace Space — Docker SDK, ZeroGPU (A10G) target.
#
# Build context: the *deploy snapshot* (assembled by README §Deploy)
# which contains both this directory's files at the root AND a copy of
# services/inference/ at services/inference/.  The `services/inference`
# COPY below is what lets `pip install -e ./services/inference[real]`
# resolve.
#
# python:3.11-slim — torch ships its own CUDA runtime via the pip wheel,
# so we don't need a CUDA base image; the host driver on ZeroGPU exposes
# the GPU through /dev/nvidia*.

FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # HF model cache.  /data is persistent on paid Spaces; on free
    # ZeroGPU /data isn't writable so we fall back to /tmp in app.py
    # if needed.  Either way, set HF_HOME so huggingface_hub doesn't
    # write under /root/.cache (which is ephemeral and per-cold-boot).
    HF_HOME=/data/hf-cache \
    HUGGINGFACE_HUB_CACHE=/data/hf-cache \
    LOG_LEVEL=INFO \
    # TRIBE transcribes by shelling out to `uvx whisperx`, whose isolated env
    # lacks the hf_transfer package; HF infra defaults this flag to 1, which
    # crashes that model download. Force it off process-wide so the subprocess
    # inherits it (proven necessary on the RunPod GPU run).
    HF_HUB_ENABLE_HF_TRANSFER=0

# System deps.
#   ffmpeg / ffprobe — duration check, trim, preprocess.
#   git              — `pip install git+https://...` for tribev2.
#   build-essential  — many transitive deps (e.g. spacy's blis) build wheels.
#   curl, ca-certificates — generic.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg \
      git \
      curl \
      ca-certificates \
      build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy ONLY what's needed for `pip install` first, so layer caching
# survives changes to app.py.  The inference package is the slow part
# (torch + tribev2 + transitive ML deps).
#
# `shared/` is the repo-root contracts package; services/inference's
# _shared.py walks `parents[3]` and imports `shared.schemas`, so the
# layout under /app must mirror the monorepo (services/inference and
# shared as siblings).
COPY shared/             /app/shared/
COPY services/inference/ /app/services/inference/
COPY services/hf-space/requirements.txt /app/requirements.txt

RUN pip install --upgrade pip \
 && pip install -r /app/requirements.txt

# TRIBE's audio transcription shells out to `uvx whisperx` at inference time,
# so `uvx` must exist in the image — install uv (provides both `uv` and `uvx`).
# Proven necessary on the RunPod GPU run. WhisperX itself + the large-v3 weights
# are fetched lazily on the first prediction, so warm the Space once after deploy.
RUN pip install uv

# Now bring in the Space code.
COPY services/hf-space/app.py /app/app.py

# HF Spaces exposes the container on $PORT, default 7860.  The
# uvicorn command honours $PORT when set.
ENV PORT=7860
EXPOSE 7860

# `--workers 1` because TribeBackend holds GPU resources we don't want
# to clone; the work is I/O bound at the HTTP layer and CPU/GPU bound
# inside the background task.  ZeroGPU allocates per-call anyway.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-7860} --workers 1 --proxy-headers"]
