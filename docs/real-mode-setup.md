# Real-mode setup — fresh GPU box to first inference

**Audience:** the user who wants to run TRIBE v2 against a few YouTube clip
segments — to verify the model and bake the demo gallery — without a local
NVIDIA GPU, on a Linux cloud box, from scratch.

**Time:** ~25 minutes elapsed (most of it is the HuggingFace cache warm-up
on the first run), ~10 minutes of attention.

**Estimated cost:** under \$0.30 to verify one clip and bake an ~8-clip
gallery. Hourly GPU rentals dominate; cleanup is on you.

Before you start, verify the local-only pieces work in mock mode — see
the root [`README.md`](../README.md). Real mode only swaps the inference
backend; the download → ffmpeg → aggregate path is unchanged.

## 1. Pick a cloud GPU provider

TRIBE v2 needs an NVIDIA GPU with **≥ 10 GB VRAM** (`services/inference/scripts/validate_real_mode.py`
enforces this). The safe default for a demo is **24 GB** (RTX 3090 / 4090
/ L40 / A100), which leaves headroom for the V-JEPA 2 video encoder, the
Wav2Vec-BERT audio encoder, and the LLaMA-3.2-3B language head co-resident.

| Provider     | Recommended instance        | Hourly price (~mid-2026) | Notes |
|--------------|-----------------------------|--------------------------|-------|
| **RunPod**   | RTX 3090, Community Cloud   | \$0.22 / hr               | Cheapest for spot/interruptible. Use the "PyTorch 2.5" template. Spot can be reclaimed; demo runs are short enough that this is fine. |
| **Lambda Labs** | RTX 6000 Ada, 24 GB     | \$0.50 / hr               | Cleanest UX, jupyter pre-installed, no setup steps beyond `pip install`. Pay for the ergonomics. |
| **Vast.ai**  | RTX 4090, community host    | \$0.18 – \$0.40 / hr      | Cheapest community pricing, large host variability. Filter to verified hosts and CUDA ≥ 12.4. |

All three give you SSH and a CUDA-12.x Linux box. Anything below a 3090
will still run but you trade VRAM headroom for cost; on an 8 GB card the
forward pass will OOM in V-JEPA 2.

> **Why not Modal / Fly GPUs?** Both are serverless and excellent if you
> want to wrap the wrapper behind an HTTP endpoint — see option 2 in
> `docs/worker-briefs/ml-inference-status.md`. They're more setup than a
> demo needs, and they won't let you SSH in to debug. The runbook below
> assumes a long-lived SSH session.

## 2. Provision and connect

Spin up an Ubuntu 22.04 (or 24.04) image with CUDA 12.x. On RunPod the
"PyTorch 2.5 / CUDA 12.4" template pre-installs the NVIDIA driver and
CUDA toolkit. SSH in:

```bash
ssh root@<instance-ip>
nvidia-smi   # verify the driver sees the GPU and reports >= 10 GB VRAM
```

If `nvidia-smi` doesn't list a device, the host didn't allocate one —
recreate the instance, don't try to fix the driver.

## 3. Install system tools

```bash
apt update && apt install -y ffmpeg git python3.11 python3.11-venv
# pipx makes the next step's `huggingface-cli` available globally
pip install -U pip yt-dlp huggingface_hub
```

`ffmpeg` is what TRIBE's `moviepy` preprocessor shells out to for frame
extraction. `yt-dlp` is what the clip-fetcher uses to download each
YouTube segment (`--download-sections`). Both are checked by
`validate_real_mode.py`.

## 4. Clone Neural Media + install the [real] extra

```bash
git clone https://github.com/<your-fork>/Neural-Media.git
cd Neural-Media
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e 'services/pipeline'
pip install -e 'services/inference[real]'  # pulls torch + tribev2 + transitive deps
```

This last step downloads ~3 GB of Python wheels (torch + CUDA runtime +
tribev2's `neuralset` / `neuraltrain` / `x_transformers` / `moviepy` /
`spacy`). On a 1 Gbps box it takes ~3 minutes.

The TRIBE upstream package isn't on PyPI; it's pulled as
`git+https://github.com/facebookresearch/tribev2` per
`services/inference/pyproject.toml`. If you see a build error on
`neuralset` or `neuraltrain` (both Meta first-party, both pinned to
`0.0.2`), check that pip is up-to-date and CUDA wheels match the host
toolkit version — `pip install torch==2.5.1+cu124 --extra-index-url
https://download.pytorch.org/whl/cu124` is the deterministic fallback.

## 5. Accept the HuggingFace licenses

TRIBE v2 pulls four model repos on first use. Three are public; one is
gated:

| Repo                              | License        | Action |
|-----------------------------------|----------------|--------|
| `facebook/tribev2`                | CC-BY-NC-4.0   | none — public |
| `facebook/vjepa2-vitg-fpc64-256`  | MIT            | none — public |
| `facebook/w2v-bert-2.0`           | MIT            | none — public |
| `meta-llama/Llama-3.2-3B`         | Llama-3.2 (NC) | **accept on huggingface.co** |

1. Make an account at [huggingface.co](https://huggingface.co/join).
2. Visit [huggingface.co/meta-llama/Llama-3.2-3B](https://huggingface.co/meta-llama/Llama-3.2-3B)
   and click *Agree and access repository*. Approval is usually
   instant (Meta auto-grants for non-commercial use); the queue can
   take an hour on bad days.
3. Create a read token at [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens).
4. Authenticate on the box:

   ```bash
   huggingface-cli login   # paste the token at the prompt
   # or, non-interactively:
   export HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```

## 6. Run the pre-flight check

This is the moment of truth — every prerequisite, in the order they fail
in practice:

```bash
python services/inference/scripts/validate_real_mode.py
```

Expected output on a correctly-configured box:

```
Neural Media — real-mode pre-flight

  ✓ Python import: torch                                      v2.5.1+cu124
  ✓ Python import: huggingface_hub                            v0.27.0
  ✓ Python import: tribev2                                    v0.1.0
  ✓ Python import: moviepy                                    v2.2.1
  ✓ Python import: soundfile                                  v0.12.1
  ✓ HuggingFace authentication                                logged in as <you>
  ✓ Public repo reachable: facebook/tribev2                   HTTP 307
  ✓ Gated repo accessible: meta-llama/Llama-3.2-3B            HTTP 307
  ✓ Public repo reachable: facebook/vjepa2-vitg-fpc64-256     HTTP 307
  ✓ Public repo reachable: facebook/w2v-bert-2.0              HTTP 307
  ✓ GPU available with >= 10 GB VRAM                          NVIDIA GeForce RTX 3090, 24.0 GB
  ✓ Disk headroom >= 25 GB (HF cache)                         312.0 GB free at /root/.cache/huggingface
  ✓ Tool on PATH: ffmpeg                                      /usr/bin/ffmpeg
  ✓ Tool on PATH: yt-dlp                                      /usr/local/bin/yt-dlp
  ✓ yt-dlp YouTube extractor                                  exit 0 on https://www.youtube.com/watch?v=dQw4w9WgXcQ

All checks passed. Real-mode inference should run end-to-end.
```

The script runs **every** check and prints the full ✓/✗ table, then a
consolidated remediation block for any failures, and exits non-zero — so
you see everything to fix in one pass. Add `--skip-network` for an offline
subset (deps, GPU, disk, tools only).

## 7. Smoke one clip → dry-run the list → bake

The product is a YouTube URL + a `[startSec, endSec)` segment
(`shared/CONTRACTS.md` §13), so there's no TikTok export to ingest. Walk
the gates in order — each is cheaper than the next:

```bash
# (b) Smoke ONE ~5s clip end-to-end (download → ffmpeg → TRIBE → aggregate).
#     The first run warms the HuggingFace weight cache (~12 GB, ~6 min on a
#     1 Gbps box — that's why §6 checks for >= 25 GB free).
python scripts/smoke_gpu_clip.py

# (c) Validate the whole curated clip list (GPU-free): each entry's YouTube
#     URL + segment against the contract. Must be all-PASS.
python scripts/build_demo_gallery.py --dry-run

# (d) Bake the real gallery — fetches only each clip's [startSec, endSec)
#     via yt-dlp --download-sections, then runs TRIBE per segment.
python scripts/build_demo_gallery.py
```

Per-segment wall time on a 24 GB 3090 is ~12 s of TRIBE forward pass plus
the segment download; an 8-clip gallery is a few minutes end-to-end. The
weight cache is one-time and reused on subsequent runs. The full
verification checklist (output range, vertex ordering, determinism) lives
in [`gpu-verification-and-gallery-bake.md`](gpu-verification-and-gallery-bake.md).

## 8. Verify, then tear down

The bake writes `apps/web/public/demo-predictions/<slug>.json` + `index.json`.
Confirm each carries the **real** model tag, not the mock sentinel:

```bash
jq -r '.modelVersion' apps/web/public/demo-predictions/*.json | sort -u
# expect a real tag (e.g. tribe-v2@<hash>), NOT tribe-v2-mock-gallery
```

Pull the JSON off the box (they're small — `scp`, or commit from the box),
then locally `pnpm --dir apps/web dev` and open `/gallery` to confirm the
brain animates against real output.

When you're done **stop or destroy the instance** — GPU hours bill by
the minute, and a forgotten 3090 is ~\$160/month. RunPod's "stop"
preserves the volume so the next run skips steps 4–5; full destroy
preserves nothing.

## Cost worked example

Verifying one clip + baking an ~8-clip gallery on a \$0.22/hr RTX 3090:

| Step                                  | Wall time | Cost     |
|---------------------------------------|-----------|----------|
| Provisioning + apt-installs           | ~3 min    | \$0.011  |
| `pip install -e [real]` (one-time)    | ~3 min    | \$0.011  |
| HF cache warm (one-time)              | ~6 min    | \$0.022  |
| Smoke clip + yt-dlp segment fetches   | ~1 min    | \$0.004  |
| TRIBE forward pass (~8 segments)      | ~2 min    | \$0.007  |
| Verify + teardown                     | ~2 min    | \$0.007  |
| **Total first run**                   | ~17 min   | **\$0.062** |
| **Subsequent runs (HF cache reused)** | ~5 min    | **\$0.018** |

Realistically budget \$0.30 for the first session including the
inevitable "wait, did I accept the license?" moment.

## Known issues

- **Llama license approval queue.** Meta usually auto-grants but can take
  up to an hour. Run `validate_real_mode.py` after clicking *Agree* — if
  the gated-repo check still 401s, you're waiting on Meta, not on us.
- **Vertex hemisphere ordering.** TRIBE outputs 20,484 cortical
  vertices; the region masks shipped with this repo assume
  `lh[0:10242]` then `rh[10242:20484]`. Upstream doesn't commit to this
  ordering in the README. On your first real run, eyeball whether the
  V1 region mass lights up at the back of the brain — if it's on the
  midline or wrong-side, see `docs/worker-briefs/ml-inference-status.md`
  "Open items" for the permutation-map fix.
- **`spacy` model download.** TRIBE's text path uses spaCy and may
  prompt for `python -m spacy download en_core_web_sm` on first
  transcription. Run that proactively if you see a `ModuleNotFoundError:
  en_core_web_sm` halfway through inference.
