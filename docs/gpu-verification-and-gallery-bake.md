# GPU run — verify real TRIBE v2 + bake the real demo gallery

A single, scoped GPU session that does **two jobs at once**:

1. **Verify real TRIBE v2** — resolve the three unverified assumptions in
   [`worker-briefs/ml-inference-status.md`](worker-briefs/ml-inference-status.md)
   (hemisphere/vertex ordering, output range / sigmoid, determinism). Until
   these are confirmed, every "real" prediction is unaudited.
2. **Bake the real demo gallery** — replace the synthetic
   `tribe-v2-mock-gallery` JSON under `apps/web/public/demo-predictions/`
   with **real** TRIBE output, so the recruiter-facing `/single/gallery`
   demo is instant *and* backed by the real model.

Real TRIBE has **never run** (no GPU on the dev machine). This is the run
that changes that. Budget a 2–4 hr session; the metered GPU time is well
under an hour if the pre-flight is done first.

> Hardware: one GPU with **≥24 GB VRAM** (A100 / L40 / A10G; RunPod, Lambda
> Labs, Colab Pro, or an Azure GPU VM funded by the student credit — quota
> permitting). CUDA 12.x, Python 3.11–3.13, ~30 GB disk for the HF cache.
> See [`real-mode-setup.md`](real-mode-setup.md) for the deeper rationale.

---

## Step 0 — Pre-flight (free, on your laptop, BEFORE renting)

Do everything that doesn't need a GPU off the clock.

1. **Accept the HF licenses** on your HuggingFace account (gated downloads):
   TRIBE v2 (CC-BY-NC-4.0), LLaMA-3.2, V-JEPA2, Wav2Vec-BERT.
2. **Run the repo-reachability pre-flight** (pure HF API calls, no GPU):
   ```bash
   python services/inference/scripts/validate_real_mode.py
   ```
   All four transitive repos must resolve. If one is gated/unaccepted, fix it
   now — not on the GPU clock.
3. **Curate the gallery clips.** The 8 entries in
   `scripts/build_demo_gallery.py` ship with **placeholder** TikTok URLs
   (`.../1111111111111110001`) — fine for mock, but real mode must download
   real videos. Replace each `url=` with a **real, license-clean** clip
   (NASA / public-domain are safest under the project's CC-BY-NC framing).
   Keep them short (≤60 s) and visually varied.
4. **Dry-run the scripts in mock mode** (catches arg/path/handoff bugs on the
   laptop, not the GPU):
   ```bash
   python scripts/predict_one_url.py "https://www.tiktok.com/@x/video/123" --mock -o /tmp/one.json
   python scripts/build_demo_gallery.py --mock-only
   ```

---

## Step 1 — Provision + install (on the GPU box)

```bash
git clone <this repo> && cd Neural-Media
python -m venv .venv && . .venv/bin/activate
pip install -e 'services/inference[real]'        # pulls tribev2 + torch (multi-GB)
python services/inference/scripts/validate_real_mode.py   # re-confirm on the box
```

---

## Step 2 — One warmup inference + the three verification checks

Run **one** real clip and watch the logs:

```bash
python scripts/predict_one_url.py "<a-real-clip-url>" -o /tmp/verify.json
```

(Real mode is the default — `--mock` is the opt-out.) The first run downloads
the TRIBE weight stack (~10 min). Then check, in order:

| # | Check | How | If wrong |
|---|-------|-----|----------|
| 1 | **Output range / sigmoid** | T2's one-shot `event=raw-output-range` INFO log prints `preds.min/mean/max` on the first real inference. | If raw values are already `[0,1]` (mean ≈ 0.5, max < 1), the sigmoid in `backend_tribe.py` is a **double-application** → replace with passthrough. If they're z-scored `[-3,3]`, leave it. |
| 2 | **Hemisphere / vertex ordering** | Follow `ml-inference-status.md §"cortical-vertex ordering check"`: dump `preds[0, :5]`, `[10239:10247]`, `[20479:20484]`; load `data/region_masks.json` and confirm V1 lands in the back of the brain on **both** hemispheres. | If lh/rh are swapped, add a `cortex = preds[:, perm]` line in the wrapper — the masks file does **not** change. |
| 3 | **Determinism** | Run the same clip twice with the same seed; diff the two NPZs. | If they drift (likely on Ampere/Hopper), add `deterministic_torch_ops: false` to the reproducibility envelope so the seed's limits are documented. |

Record the verdicts in `ml-inference-status.md` (it's written to be updated
on the first successful GPU run).

---

## Step 3 — Bake the real gallery

With the verification green and the curated real URLs in place:

```bash
python scripts/build_demo_gallery.py     # real is default; per-clip fallback to mock on download failure
```

This writes `apps/web/public/demo-predictions/<slug>.json` + `index.json`.
Confirm the `modelVersion` in each is the **real** tag (e.g. `tribe-v2@…`),
**not** `tribe-v2-mock-gallery`. Pull the JSON off the box (they're small;
`git diff` / scp / commit from the box).

---

## Step 4 — Commit + verify in the app

```bash
git checkout -b feat/real-demo-gallery
git add apps/web/public/demo-predictions scripts/build_demo_gallery.py
git commit -m "feat(gallery): bake real TRIBE v2 activations for the demo gallery"
```

Then locally: `make dev-web`, open `/single/gallery`, click a clip — the brain
should animate against real output, and the gallery's mock badge/label
(driven by `modelVersion.startsWith("tribe-v2-mock")`) should now be **absent**.
Hand the branch to the brain terminal to merge.

---

## Cost / teardown

A 2–4 hr session is ~$1–3 on RunPod spot, within a Colab Pro session, or a
rounding error against the Azure student credit. **Deallocate the GPU the
moment you're done** — an idle GPU VM is the only real cost risk here.

## What this unblocks

- The recruiter demo (`/single/gallery`) becomes real-model-backed + instant.
- The live `/single` URL-paste path (AWS + HF Space) inherits the verified
  wrapper — see [`single-video-deploy.md`](single-video-deploy.md) for that
  deploy, which you can do separately.
