# GPU run — verify real TRIBE v2 + bake the real demo gallery

A single, scoped GPU session that does **two jobs at once**:

1. **Verify real TRIBE v2** — resolve the three unverified assumptions in
   [`worker-briefs/ml-inference-status.md`](worker-briefs/ml-inference-status.md)
   (hemisphere/vertex ordering, output range / sigmoid, determinism). Until
   these are confirmed, every "real" prediction is unaudited.
2. **Bake the real demo gallery** — replace the synthetic
   `tribe-v2-mock-gallery` JSON under `apps/web/public/demo-predictions/`
   with **real** TRIBE output, so the recruiter-facing **`/gallery`** demo is
   instant *and* backed by the real model.

Real TRIBE has **never run** (no GPU on the dev machine). This is the run
that changes that. Budget a 2–4 hr session; the metered GPU time is well
under an hour if the pre-flight is done first.

> Hardware: one GPU with **≥24 GB VRAM** (A100 / L40 / A10G; RunPod, Lambda
> Labs, Colab Pro, or an Azure GPU VM funded by the student credit — quota
> permitting). CUDA 12.x, Python 3.11–3.13, ~30 GB disk for the HF cache.
> See [`real-mode-setup.md`](real-mode-setup.md) for the deeper rationale.

---

## The exact order (don't skip — each gate is cheaper than the next)

```bash
# (a) preflight   — CUDA, TRIBE weights + HF repos, ffmpeg/yt-dlp, disk/VRAM
python services/inference/scripts/validate_real_mode.py

# (b) smoke clip  — ONE ~5s segment end-to-end through the real pipeline
python scripts/smoke_gpu_clip.py

# (c) dry-run     — validate the whole clip list (no GPU, no network)
python scripts/build_demo_gallery.py --dry-run

# (d) full bake   — only once (a)–(c) are green
python scripts/build_demo_gallery.py
```

Each step fails in **seconds** with an exact reason, so a bad GPU, an
unaccepted license, or a typo'd clip URL never costs you a 10-minute bake.
`validate_real_mode.py` runs every check and prints a ✓/✗ table; `--dry-run`
is GPU-free (run it on the laptop too) but is listed after the smoke clip
because the *list* only matters once you've proven *one* clip works. The
detailed walkthrough below expands each gate.

---

## Step 0 — Pre-flight (free, on your laptop, BEFORE renting)

Do everything that doesn't need a GPU off the clock.

1. **Accept the HF licenses** on your HuggingFace account: LLaMA-3.2 is the
   only gated one (TRIBE v2, V-JEPA2, Wav2Vec-BERT download freely). Have a
   **Read** access token ready.
2. **Run the pre-flight.** It runs *every* check and prints a ✓/✗ table
   (Python deps, HF auth + repo reachability, CUDA, disk/VRAM headroom,
   ffmpeg/yt-dlp), then a consolidated remediation block. On the laptop the
   local checks pass and the GPU/network ones tell you exactly what to fix:
   ```bash
   python services/inference/scripts/validate_real_mode.py   # add --skip-network when offline
   ```
   Fix every ✗ now — not on the GPU clock.
3. **Wire the curated gallery clips.** The gallery is now **YouTube clip +
   timestamp segment** (see `shared/CONTRACTS.md` §13). Each entry in
   `scripts/build_demo_gallery.py` is `(YouTube URL, startSec, endSec)` with
   `endSec - startSec ≤ 90`. Plug in your ~8 preselected clips + their
   segments, picking varied content (visual / faces / on-screen text / music)
   so the brain maps differ clip-to-clip. *(The brain terminal usually wires
   these for you.)*
4. **Dry-run the clip list** (no GPU, no network) — validates every entry's
   YouTube URL + segment against the contract and prints a PASS/FAIL table.
   It must be **all-PASS** before you bake:
   ```bash
   python scripts/build_demo_gallery.py --dry-run
   ```
   Until you've wired real clips this FAILS on the `REPLACE_ME` placeholders —
   that's the gate doing its job. (`--mock-only` separately proves the bake
   tooling end-to-end with synthetic output.)

---

## Step 1 — Provision + install (on the GPU box)

```bash
git clone <this repo> && cd Neural-Media
python -m venv .venv && . .venv/bin/activate
pip install -e 'services/inference[real]'        # pulls tribev2 + torch (multi-GB)
hf auth login                                     # paste your Read token (gated LLaMA)
python services/inference/scripts/validate_real_mode.py   # re-confirm on the box
```

---

## Step 2 — Smoke ONE clip, then verify

First prove the per-clip pipeline works end-to-end on a single short segment
(download → ffmpeg → TRIBE → 8-region aggregate). The first run downloads the
TRIBE weight stack (~10 min):

```bash
python scripts/smoke_gpu_clip.py        # default clip + ~5s segment; prints the output shape
```

A green **"smoke OK"** (T timepoints × 8 regions, uniform series lengths)
means weights + GPU + extractor + aggregate all work. Now run one longer real
segment through the predictor and do the three verification checks:

```bash
python scripts/predict_one_url.py "<a-real-youtube-url>" --start-sec 0 --end-sec 30 -o /tmp/verify.json
```

Check, in order:

| # | Check | How | If wrong |
|---|-------|-----|----------|
| 1 | **Output range / sigmoid** | T2's one-shot `event=raw-output-range` INFO log prints `preds.min/mean/max` on the first real inference. | If raw values are already `[0,1]` (mean ≈ 0.5, max < 1), the sigmoid in `backend_tribe.py` is a **double-application** → replace with passthrough. If they're z-scored `[-3,3]`, leave it. |
| 2 | **Hemisphere / vertex ordering** | Follow `ml-inference-status.md §"cortical-vertex ordering check"`: dump `preds[0, :5]`, `[10239:10247]`, `[20479:20484]`; load `data/region_masks.json` and confirm V1 lands in the back of the brain on **both** hemispheres. | If lh/rh are swapped, add a `cortex = preds[:, perm]` line in the wrapper — the masks file does **not** change. |
| 3 | **Determinism** | Run the same segment twice with the same seed; diff the two NPZs. | If they drift (likely on Ampere/Hopper), add `deterministic_torch_ops: false` to the reproducibility envelope so the seed's limits are documented. |

Record the verdicts in `ml-inference-status.md` (it's written to be updated
on the first successful GPU run).

---

## Step 3 — Dry-run the list, then bake the real gallery

With the verification green and your YouTube clips + segments wired in,
re-validate the whole list on the box (cheap, no GPU), then bake:

```bash
python scripts/build_demo_gallery.py --dry-run   # must be all-PASS
python scripts/build_demo_gallery.py             # real is default; fetches only each clip's [startSec,endSec) via yt-dlp --download-sections
```

This writes `apps/web/public/demo-predictions/<slug>.json` + `index.json`.
Confirm the `modelVersion` in each is the **real** tag (e.g. `tribe-v2@…`),
**not** `tribe-v2-mock-gallery`. Pull the JSON off the box (they're small —
download via Jupyter, or commit from the box).

---

## Step 4 — Commit + verify in the app

```bash
git checkout -b feat/real-demo-gallery
git add apps/web/public/demo-predictions scripts/build_demo_gallery.py
git commit -m "feat(gallery): bake real TRIBE v2 activations for the demo gallery"
```

Then locally: `make dev-web`, open **`/gallery`**, click a clip — the brain
should animate against real output, and the gallery's mock label (driven by
`modelVersion.startsWith("tribe-v2-mock")`) should now be **absent**. Hand the
branch to the brain terminal to merge.

---

## Cost / teardown

A 2–4 hr session is ~$1–3 on RunPod spot, within a Colab Pro session, or a
rounding error against the Azure student credit. **Deallocate the GPU the
moment you're done** — an idle GPU is the only real cost risk here.

## What this unblocks

- The recruiter demo (`/gallery`) becomes real-model-backed + instant.
- The live single-video URL path (AWS + HF Space) inherits the verified
  wrapper — see [`single-video-deploy.md`](single-video-deploy.md) for that
  deploy, which you can do separately.
