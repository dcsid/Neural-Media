"""Pre-flight check for TRIBE v2 real-mode inference.

Run this BEFORE attempting `--no-mock`. Walks the prerequisite stack in
the order things fail in practice: Python deps -> HF auth -> license
gates -> GPU -> disk headroom -> system tools -> yt-dlp YouTube extractor.
Runs **every** check and prints a ✓/✗ table, then a consolidated
remediation block for the failures, so a pod operator sees everything to
fix in one pass instead of fix-one-rerun-repeat. Exits nonzero if any
check failed.

Usage::

    python services/inference/scripts/validate_real_mode.py

    # Probe yt-dlp against a specific YouTube URL instead of the default:
    python services/inference/scripts/validate_real_mode.py \\
        --video-url https://www.youtube.com/watch?v=dQw4w9WgXcQ

    # Skip the network checks entirely (offline CI):
    python services/inference/scripts/validate_real_mode.py --skip-network

Exit codes::

    0    all checks passed
    1    one or more checks failed (see the remediation block)
    2    an internal error while running a check

This script intentionally has zero project imports: it must be runnable
from a fresh clone before `pip install -e '.[real]'` succeeds, so it
can only assume the stdlib.
"""

from __future__ import annotations

import argparse
import importlib
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

# A stable, public YouTube video used to probe yt-dlp's extractor without
# downloading (--simulate). The product is YouTube-only after the §13
# refocus, so the probe targets YouTube (not the old TikTok share host).
# dQw4w9WgXcQ is the canonical long-lived example used in CONTRACTS.md §13.
_DEFAULT_PROBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# Repos TRIBE v2 pulls transitively on first use. Sources:
#   * model card: huggingface.co/facebook/tribev2 README
#   * round-1 status: docs/worker-briefs/ml-inference-status.md
# Llama-3.2-3B is the only gated one (license acceptance required); the
# rest are public/ungated but we still probe them so a wrong repo id or
# revoked weights surface here, not 30 minutes into a real run.
_TRIBE_REPO = "facebook/tribev2"
_TRANSITIVE_REPOS: tuple[tuple[str, bool], ...] = (
    # (repo_id, is_license_gated)
    ("facebook/tribev2", False),
    ("meta-llama/Llama-3.2-3B", True),
    ("facebook/vjepa2-vitg-fpc64-256", False),
    ("facebook/w2v-bert-2.0", False),
)

# Minimum VRAM that the brief calls out for a full TRIBE forward pass.
# The status doc gives 10-20GB as the realistic window; pick the floor so
# a 12GB card (e.g., RTX 3060) clears but a 8GB card (e.g., RTX 3070) does
# not. Real demo box is the 24GB RTX 3090/4090 (see docs/real-mode-setup.md).
_MIN_VRAM_BYTES = 10 * 1024**3

# TRIBE + its transitive weights (Llama-3.2-3B ~6 GB, V-JEPA2 ViT-g ~7 GB,
# Wav2Vec-BERT ~2.4 GB, the tribev2 readout) land in the HuggingFace cache on
# first run, on top of per-clip video tempfiles + activation outputs. 25 GB
# free clears the weight stack with headroom; a small pod root disk that fills
# mid-download otherwise fails with a cryptic errno 28 deep in a stack trace.
_MIN_FREE_DISK_BYTES = 25 * 1024**3

# Visual width for the check-name column. Keep aligned with the longest
# check label below ("Gated repo accessible: meta-llama/Llama-3.2-3B").
_LABEL_WIDTH = 56


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a single check.

    ``remediation`` is None on pass, and a short shell-friendly snippet
    on fail. ``detail`` is an optional one-liner shown next to the ✓ on
    pass (e.g., the resolved torch version) so the user can confirm the
    runtime they got matches the runtime they expected.
    """

    name: str
    passed: bool
    detail: str | None = None
    remediation: str | None = None


# ----------------------------------------------------------------------
# Output helpers
# ----------------------------------------------------------------------


def _emit(result: CheckResult) -> None:
    """Print one check result. ✓/✗ + label + optional detail.

    Always flushes so a Ctrl-C mid-run leaves a coherent checklist on
    screen instead of buffered nothing.
    """
    mark = "✓" if result.passed else "✗"
    line = f"  {mark} {result.name:<{_LABEL_WIDTH}}"
    if result.detail:
        line += f"  {result.detail}"
    print(line, flush=True)


# ----------------------------------------------------------------------
# Individual checks. Each returns a CheckResult; the runner runs them all
# and aggregates the failures.
# ----------------------------------------------------------------------


def check_python_dep(module: str, install_hint: str) -> CheckResult:
    """Verify `module` is importable. Used for torch / huggingface_hub /
    tribev2 / moviepy / soundfile. Reports the resolved version when one
    is available so the user can confirm the pin TRIBE upstream wants."""
    name = f"Python import: {module}"
    try:
        m = importlib.import_module(module)
    except ImportError as exc:
        return CheckResult(
            name=name,
            passed=False,
            remediation=(
                f"# {exc}\n"
                "pip install -e 'services/inference[real]'\n"
                f"# (or: pip install {install_hint})"
            ),
        )
    version = getattr(m, "__version__", None) or "?"
    return CheckResult(name=name, passed=True, detail=f"v{version}")


def check_hf_whoami() -> CheckResult:
    """`huggingface_hub.whoami()` requires a token in the environment or
    cached from `huggingface-cli login`. Without it, every gated download
    later in the pipeline will 401 with a misleading 'repo not found'."""
    name = "HuggingFace authentication"
    try:
        from huggingface_hub import whoami  # type: ignore[import-untyped]
    except ImportError:
        return CheckResult(
            name=name,
            passed=False,
            remediation="pip install huggingface_hub",
        )
    try:
        info = whoami()
    except Exception as exc:  # huggingface_hub raises a few distinct shapes
        return CheckResult(
            name=name,
            passed=False,
            remediation=(
                f"# {exc.__class__.__name__}: {exc}\n"
                "huggingface-cli login\n"
                "# (or set HF_TOKEN in your environment to a read token from\n"
                "#  https://huggingface.co/settings/tokens)"
            ),
        )
    return CheckResult(
        name=name, passed=True, detail=f"logged in as {info.get('name', '?')}"
    )


def check_repo_accessible(repo_id: str, gated: bool) -> CheckResult:
    """HEAD the canonical resolve URL with the user's HF token.

    For ungated repos a 200/302/307 is the pass signal — the repo exists
    and the CDN handed back a download URL.

    For gated repos (meta-llama/Llama-3.2-*) the same 200/302/307 means
    'token has been authorized for this repo' i.e. the user has accepted
    the license. A 401 with `x-error-code: GatedRepo` is the unaccepted-
    license signal — direct the user to the license-accept page instead
    of muttering 'auth required'.

    All HEAD requests go to <repo>/resolve/main/config.json because every
    HF model repo has one and HEAD is bandwidth-free.
    """
    name = ("Gated repo accessible: " if gated else "Public repo reachable: ") + repo_id
    url = f"https://huggingface.co/{repo_id}/resolve/main/config.json"

    # facebook/tribev2 is the one exception: its custom checkpoint repo
    # has no config.json (round 1 noted `config.json` 404s). Hit
    # `config.yaml` for that single repo so the HEAD matches a real path.
    if repo_id == "facebook/tribev2":
        url = f"https://huggingface.co/{repo_id}/resolve/main/config.yaml"

    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
    req = urllib.request.Request(url, method="HEAD")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    # HEAD on resolve URLs returns 302/307 to the CDN. Don't follow —
    # the redirect itself confirms permission, and following burns time.
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(req, timeout=15) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        err_code = exc.headers.get("x-error-code", "")
        # urllib raises HTTPError for 3xx when the redirect handler returns
        # None (our _NoRedirect does exactly that — see its docstring). For
        # us a 301/302/307 on a resolve URL IS the pass signal: it means HF
        # accepted the request and would redirect us to the CDN. We didn't
        # want the bytes; we wanted the verdict.
        if status in (301, 302, 307):
            return CheckResult(name=name, passed=True, detail=f"HTTP {status}")
        if status == 401 and err_code == "GatedRepo":
            return CheckResult(
                name=name,
                passed=False,
                remediation=(
                    f"# License not accepted for {repo_id}.\n"
                    f"open https://huggingface.co/{repo_id}\n"
                    "# click 'Agree and access repository', then re-run this\n"
                    "# script. Your HF token must belong to the same account\n"
                    "# that accepted the license."
                ),
            )
        if status == 401:
            return CheckResult(
                name=name,
                passed=False,
                remediation=(
                    f"# HTTP 401 on {repo_id}. Token missing, expired, or wrong account.\n"
                    "huggingface-cli login\n"
                    "# (or: export HF_TOKEN=<token from https://huggingface.co/settings/tokens>)"
                ),
            )
        if status == 404:
            return CheckResult(
                name=name,
                passed=False,
                remediation=(
                    f"# HTTP 404 on {repo_id}. Repo id may have been renamed since the\n"
                    "# last status pass. Check the upstream model card and update\n"
                    "# _TRANSITIVE_REPOS in this script + the wrapper accordingly."
                ),
            )
        return CheckResult(
            name=name,
            passed=False,
            remediation=f"# Unexpected HTTP {status} on {repo_id}.\n# Check network / HF status.",
        )
    except urllib.error.URLError as exc:
        return CheckResult(
            name=name,
            passed=False,
            remediation=f"# Network error: {exc.reason}\n# Check your internet connection.",
        )

    if status in (200, 302, 307):
        return CheckResult(name=name, passed=True, detail=f"HTTP {status}")
    return CheckResult(
        name=name,
        passed=False,
        remediation=f"# Unexpected HTTP {status} on {repo_id}.",
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Don't follow 302/307 — the redirect itself is the pass signal."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def check_gpu() -> CheckResult:
    """`torch.cuda.is_available()` AND >= 10 GB device memory.

    macOS has no CUDA; MPS is not supported by the TRIBE backbones at
    the time of writing (the V-JEPA2 and Wav2Vec-BERT forward passes
    use CUDA-only ops). A real-mode run must be on a Linux box with
    an NVIDIA card."""
    name = "GPU available with >= 10 GB VRAM"
    try:
        import torch  # type: ignore[import-not-found]
    except ImportError:
        return CheckResult(
            name=name,
            passed=False,
            remediation="pip install -e 'services/inference[real]'",
        )
    if not torch.cuda.is_available():
        return CheckResult(
            name=name,
            passed=False,
            remediation=(
                "# No CUDA device. TRIBE v2 needs an NVIDIA GPU.\n"
                "# macOS / MPS is not supported by the V-JEPA2 and Wav2Vec-BERT\n"
                "# backbones TRIBE depends on. See docs/real-mode-setup.md for\n"
                "# cloud-GPU options (RunPod / Lambda Labs / Vast.ai, ~$0.30/hr)."
            ),
        )
    props = torch.cuda.get_device_properties(0)
    vram = int(props.total_memory)
    if vram < _MIN_VRAM_BYTES:
        return CheckResult(
            name=name,
            passed=False,
            detail=f"{props.name}, {vram / 1024**3:.1f} GB",
            remediation=(
                f"# Device 0 ({props.name}) has only {vram / 1024**3:.1f} GB VRAM;\n"
                f"# TRIBE needs >= {_MIN_VRAM_BYTES / 1024**3:.0f} GB. Pick a 24GB\n"
                "# card (RTX 3090 / 4090 / L40 / A100). See docs/real-mode-setup.md."
            ),
        )
    return CheckResult(
        name=name,
        passed=True,
        detail=f"{props.name}, {vram / 1024**3:.1f} GB",
    )


def check_disk_headroom() -> CheckResult:
    """Free disk on the filesystem that will hold the HuggingFace cache.

    The weight stack is multi-GB; a small pod root disk fills mid-download
    and torch / yt-dlp fail with a confusing errno 28 deep in a stack trace.
    Resolves the HF cache dir (HF_HUB_CACHE / HF_HOME / the default
    ~/.cache/huggingface), walking up to the nearest existing parent so the
    free-space probe works before the cache dir is created."""
    name = f"Disk headroom >= {_MIN_FREE_DISK_BYTES // 1024**3} GB (HF cache)"
    cache_dir = (
        os.environ.get("HF_HUB_CACHE")
        or os.environ.get("HF_HOME")
        or os.path.expanduser("~/.cache/huggingface")
    )
    probe = cache_dir
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError as exc:
        return CheckResult(
            name=name,
            passed=False,
            remediation=f"# Couldn't stat the cache filesystem at {probe}: {exc}",
        )
    detail = f"{free / 1024**3:.1f} GB free at {probe}"
    if free < _MIN_FREE_DISK_BYTES:
        return CheckResult(
            name=name,
            passed=False,
            detail=detail,
            remediation=(
                f"# Only {free / 1024**3:.1f} GB free where the HF cache lives ({probe}).\n"
                f"# TRIBE's weight stack needs >= {_MIN_FREE_DISK_BYTES // 1024**3} GB. Mount a\n"
                "# bigger volume or point the cache at one: export HF_HOME=/workspace/hf"
            ),
        )
    return CheckResult(name=name, passed=True, detail=detail)


def check_tool_on_path(tool: str, install_hint: str) -> CheckResult:
    """ffmpeg + yt-dlp must resolve via `shutil.which`. ffmpeg is the
    decoder TRIBE's moviepy preprocessor shells out to; yt-dlp is the
    downloader. Neither is bundled with the Python deps."""
    name = f"Tool on PATH: {tool}"
    path = shutil.which(tool)
    if not path:
        return CheckResult(name=name, passed=False, remediation=install_hint)
    return CheckResult(name=name, passed=True, detail=path)


def check_yt_dlp_extractor(url: str) -> CheckResult:
    """Probe yt-dlp's YouTube extractor without downloading.

    `--simulate` exercises the extractor (and any age/bot gating) without
    fetching media bytes, so it's bandwidth-cheap. If yt-dlp's YouTube
    extractor is stale or the host is gating this IP, this fires here —
    not 10 minutes into the gallery bake."""
    name = "yt-dlp YouTube extractor"
    yt_dlp = shutil.which("yt-dlp")
    if not yt_dlp:
        return CheckResult(
            name=name,
            passed=False,
            remediation="pip install yt-dlp  # (or: brew install yt-dlp)",
        )
    try:
        proc = subprocess.run(
            [yt_dlp, "--simulate", "--quiet", "--no-warnings", url],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name=name,
            passed=False,
            remediation=(
                "# yt-dlp probe timed out (>60s). TikTok may be throttling\n"
                "# this IP. Retry from a different network or wait an hour."
            ),
        )
    if proc.returncode != 0:
        # yt-dlp's stderr already prints the failure mode. Surface a clipped
        # version so the user doesn't have to re-run with --verbose.
        tail = (proc.stderr or "").strip().splitlines()
        snippet = "\n".join(tail[-3:]) if tail else "(no stderr)"
        return CheckResult(
            name=name,
            passed=False,
            remediation=(
                f"# yt-dlp exit {proc.returncode} on {url}\n"
                f"# {snippet}\n"
                "# If the extractor changed, upgrade: pip install -U yt-dlp"
            ),
        )
    return CheckResult(name=name, passed=True, detail=f"exit 0 on {url}")


# ----------------------------------------------------------------------
# Runner
# ----------------------------------------------------------------------


def _build_checks(probe_url: str | None) -> list[Callable[[], CheckResult]]:
    """Return the checks in the order they should run.

    Order matters: a missing `torch` import would also fail the GPU check,
    so deps come first and the runner short-circuits on the first failure.
    Network probes come after local checks so an offline user gets a clean
    'install torch' message instead of a confusing TLS error.
    """
    deps = [
        ("torch", "torch>=2.5.1,<2.7"),
        ("huggingface_hub", "huggingface_hub>=0.24"),
        ("tribev2", "git+https://github.com/facebookresearch/tribev2"),
        # TRIBE upstream uses moviepy for video frame extraction (not decord
        # — confirmed against facebookresearch/tribev2 pyproject.toml).
        ("moviepy", "moviepy>=2.2.1"),
        ("soundfile", "soundfile"),
    ]
    checks: list[Callable[[], CheckResult]] = [
        (lambda mod=mod, hint=hint: check_python_dep(mod, hint)) for mod, hint in deps
    ]
    checks.append(check_hf_whoami)
    for repo_id, gated in _TRANSITIVE_REPOS:
        checks.append(lambda rid=repo_id, g=gated: check_repo_accessible(rid, g))
    checks.append(check_gpu)
    checks.append(check_disk_headroom)
    checks.append(
        lambda: check_tool_on_path(
            "ffmpeg", "apt install ffmpeg  # (or: brew install ffmpeg)"
        )
    )
    checks.append(
        lambda: check_tool_on_path("yt-dlp", "pip install yt-dlp  # (or: brew install yt-dlp)")
    )
    if probe_url is not None:
        checks.append(lambda: check_yt_dlp_extractor(probe_url))
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Pre-flight check for TRIBE v2 real-mode inference. Aborts on the "
            "first failing check with a copy-pasteable remediation."
        )
    )
    parser.add_argument(
        "--video-url",
        default=_DEFAULT_PROBE_URL,
        help=(
            "TikTok share URL to probe yt-dlp against (default: a stable known "
            "share URL the data-pipeline downloader docstring already cites). "
            "Use this if the default 404s, e.g. the video was removed."
        ),
    )
    parser.add_argument(
        "--skip-network",
        action="store_true",
        help=(
            "Skip every check that hits the network: huggingface.co HEADs and "
            "the yt-dlp probe. Use in offline CI."
        ),
    )
    args = parser.parse_args(argv)

    print("Neural Media — real-mode pre-flight\n")

    if args.skip_network:
        # Offline mode runs only local checks (imports, GPU, tools on PATH).
        # No HF auth, no repo HEADs, no yt-dlp probe.
        local_only = [
            (lambda mod=mod, hint=hint: check_python_dep(mod, hint))
            for mod, hint in (
                ("torch", "torch>=2.5.1,<2.7"),
                ("huggingface_hub", "huggingface_hub>=0.24"),
                ("tribev2", "git+https://github.com/facebookresearch/tribev2"),
                ("moviepy", "moviepy>=2.2.1"),
                ("soundfile", "soundfile"),
            )
        ]
        local_only.append(check_gpu)
        local_only.append(check_disk_headroom)
        local_only.append(
            lambda: check_tool_on_path(
                "ffmpeg", "apt install ffmpeg  # (or: brew install ffmpeg)"
            )
        )
        local_only.append(
            lambda: check_tool_on_path(
                "yt-dlp", "pip install yt-dlp  # (or: brew install yt-dlp)"
            )
        )
        checks = local_only
    else:
        checks = _build_checks(args.video_url)

    # Run EVERY check (no abort-on-first) so the operator sees the whole
    # ✓/✗ table in one pass, then a consolidated remediation block.
    failed: list[CheckResult] = []
    internal_error = False
    for run in checks:
        try:
            result = run()
        except Exception as exc:  # noqa: BLE001 — surface the failure, don't crash
            result = CheckResult(
                name="internal error while running a check",
                passed=False,
                detail=f"{exc.__class__.__name__}: {exc}",
            )
            internal_error = True
        _emit(result)
        if not result.passed:
            failed.append(result)

    if failed:
        print()
        print(f"{len(failed)} check(s) FAILED:")
        for result in failed:
            print(f"  ✗ {result.name}")
            if result.remediation:
                print("    Remediation:")
                for line in result.remediation.strip("\n").splitlines():
                    print(f"      {line}")
        return 2 if internal_error else 1

    print("\nAll checks passed. Real-mode inference should run end-to-end.")
    print("Note: this script doesn't actually load TRIBE weights into VRAM — the")
    print("first real `--no-mock` run will still spend a few minutes warming the")
    print("HuggingFace cache. See docs/real-mode-setup.md for the full runbook.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
