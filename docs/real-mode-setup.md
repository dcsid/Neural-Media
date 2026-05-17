# Real-mode setup — fresh GPU box to first inference

**Audience:** the user who wants to run TRIBE v2 against their TikTok
history without a local NVIDIA GPU, on a Linux cloud box, from scratch.

**Time:** ~25 minutes elapsed (most of it is the HuggingFace cache warm-up
on the first run), ~10 minutes of attention.

**Estimated cost:** under \$0.30 to process a 30-video demo (1 hour of
watch history). Hourly GPU rentals dominate; cleanup is on you.

Before you start, verify the local-only pieces work in mock mode — see
the root [`README.md`](../README.md). Real mode does not change the
frontend or the importer; it only swaps the inference backend.

## 1. Pick a cloud GPU provider

TRIBE v2 needs an NVIDIA GPU with **≥ 10 GB VRAM** (`scripts/validate_real_mode.py`
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
extraction. `yt-dlp` is what the data-pipeline uses to fetch TikTok
videos. Both are checked by `validate_real_mode.py`.

## 4. Clone Neural Media + install the [real] extra

```bash
git clone https://github.com/<your-fork>/Neural-Media.git
cd Neural-Media
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e 'services/pipeline'
pip install -e 'services/api'
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
  ✓ Tool on PATH: ffmpeg                                      /usr/bin/ffmpeg
  ✓ Tool on PATH: yt-dlp                                      /usr/local/bin/yt-dlp
  ✓ yt-dlp share-URL extractor                                exit 0 on https://www.tiktokv.com/share/video/7640163791312801054/

All checks passed. Real-mode inference should run end-to-end.
```

If a check fails the script prints a copy-pasteable remediation and
exits non-zero. Run it again after each fix; it short-circuits on the
first failure so you only fix one thing at a time.

## 7. Run the pipeline against your TikTok export

Copy your `Watch History.txt` (or `user_data.json`, or the full export
zip) onto the box via `scp`, then:

```bash
make init-db
python -m neural_media_pipeline \
  --hours 1 \
  --purge-after-inference \
  --no-mock \
  /root/Watch\ History.txt
```

What happens on the first run:

1. **HuggingFace cache warms.** TRIBE downloads its weights, then the
   transitive Llama / V-JEPA 2 / Wav2Vec-BERT weights. ~12 GB total to
   `~/.cache/huggingface/`. **~6 minutes on a 1 Gbps box.** Provision at
   least 30 GB of disk.
2. **yt-dlp downloads** the videos from the time window, one every
   ~2 seconds against TikTok's rate limit (`--hours 1` is ~30 videos =
   ~1 minute of downloads).
3. **TRIBE runs the forward pass** per video. Per-video wall time on a
   24 GB 3090 is ~12 seconds (~6 minutes for 30 videos).
4. **`--purge-after-inference`** deletes each raw and preprocessed .mp4
   the moment its `RegionMetrics` land. Peak transient disk is ~10 MB
   regardless of batch size; the SQLite catalogue + activation NPZs are
   what you keep.

For a 30-video demo: ~13 minutes wall clock end-to-end, ~5 GB peak GPU
memory in use, ~12 GB of HuggingFace cache on disk (one-time, reused on
subsequent runs).

## 8. Verify, then tear down

```bash
make dev-api &                          # local API on 127.0.0.1:8000
curl http://127.0.0.1:8000/videos | jq  # confirm rows landed
```

Forward the API port via SSH (`ssh -L 8000:127.0.0.1:8000 root@<ip>`)
and load the dashboard from your laptop browser to validate the full
trip end-to-end.

When you're done **stop or destroy the instance** — GPU hours bill by
the minute, and a forgotten 3090 is ~\$160/month. RunPod's "stop"
preserves the volume so the next run skips step 4–5; full destroy
preserves nothing.

## Cost worked example

A 30-video demo on a \$0.22/hr RTX 3090:

| Step                                  | Wall time | Cost     |
|---------------------------------------|-----------|----------|
| Provisioning + apt-installs           | ~3 min    | \$0.011  |
| `pip install -e [real]` (one-time)    | ~3 min    | \$0.011  |
| HF cache warm (one-time)              | ~6 min    | \$0.022  |
| yt-dlp download (30 videos)           | ~1 min    | \$0.004  |
| TRIBE forward pass (30 videos)        | ~6 min    | \$0.022  |
| Verify + teardown                     | ~2 min    | \$0.007  |
| **Total first run**                   | ~21 min   | **\$0.077** |
| **Subsequent runs (HF cache reused)** | ~9 min    | **\$0.033** |

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
