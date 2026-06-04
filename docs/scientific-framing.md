# Scientific framing — what Neural Media does and does not measure

This document is binding for every worker. Copy that holds up to scrutiny is
a product feature, not a marketing one. If something here changes, it
changes in this file first.

## What the tool DOES measure

- **Predicted BOLD fMRI response** (cortical surface, 20,484 vertices) for a
  chosen video segment of up to 90 seconds, from the **average** of TRIBE v2's
  720 training subjects.
- Predicted activation in eight well-localized cortical regions: V1–V4,
  auditory cortex, the language network, FFA, and VWFA.
- Comparative reads **between clips** — "clip A predicts higher V1 than
  clip B" — rather than absolute magnitudes, which aren't calibrated to
  anything physical.

## What it does NOT measure (do not claim these)

- **Individual brain response.** The model predicts an average, not the
  specific user's brain.
- **Subcortical reward / "addiction" circuitry.** Nucleus accumbens, VTA,
  etc. are not in the output space.
- **Cumulative effects, habituation, or neuroplasticity** over time.
- **Subjective states**: aesthetic preference, confusion, cognitive
  overload, memory encoding, satisfaction.
- **"How online video is rewiring your brain"** or any rhetoric in that family.

## Rules for copy and UI text

1. Use "predicted average cortical response" not "your brain response."
2. Use "engagement" only in the predicted-activation sense; never as a
   stand-in for attention, addiction, reward, or affect.
3. Prefer comparative claims ("video A predicts higher V1 than video B")
   over absolute claims ("video A engages V1 at 0.74"). Absolute
   activation magnitudes are not calibrated to anything physical.
4. Mention TRIBE v2's CC-BY-NC license in the README and the app footer.
5. **When mock mode produced the displayed data, label it.** See below.

## Mock mode — what it is and what it isn't

TRIBE needs a GPU, so two backends sit behind the same contract:

- **`real`** — actually runs TRIBE v2 on the downloaded segment. Predictions
  are grounded in the clip's frames + audio. This is what the deployed
  HuggingFace Space runs.
- **`mock`** — `MockBackend`
  (`services/inference/neural_media_inference/backend.py`), **a sine wave per
  cortical vertex seeded by `SHA-256(video_id, seed)`.** No GPU, no network, no
  model weights. It powers the tests, local dev
  (`services/hf-space/mock_local.py`), and the gallery-bake fallback when a
  real run isn't available.

In mock mode:

- The video is never downloaded. yt-dlp is not invoked.
- The video file is never opened, decoded, or read. Frames and audio
  are not examined.
- The output is a deterministic function of the URL *string*. Two URLs
  pointing to wildly different videos produce different mock activations
  only because the URL strings differ — not because the content differs.
- Feeding the same URL twice produces byte-identical activations,
  regardless of whether the underlying video ever existed.

This is intentional: mock mode exercises the full plumbing — fetch → infer →
aggregate → callback → 3D render — without a GPU, and makes the example gallery
bulletproof. It is the right tool for tests, system demos, and any case where
the integrity of the *prediction* does not matter.

It is **not** a prediction about the video's actual content. Specifically:

- Mock region bars and brain-mesh colors are honest aggregations of dishonest
  inputs. The reduction math is correct; the vertex values being reduced are
  URL-hash sine waves.
- The reproducibility envelope (CONTRACTS.md §9) still records everything
  correctly — a `modelVersion` / `model_id` beginning `tribe-v2-mock` is the
  truth signal. Any consumer that shows mock output as a real prediction is
  mis-labeling.

**UI requirement:** wherever the app (the live page or a gallery entry) renders
data produced by the mock backend — a result whose `modelVersion` / `model_id`
begins `tribe-v2-mock` — the user must see a visible label saying so. The
frontend owns this label; do not strip it.

## Reproducibility envelope

Every `InferenceRun` MUST log:

- model id and version (e.g. `tribe-v2 / 2024-09-rev3`)
- random seed
- preprocessing params (resolution, fps, audio sample rate)
- TRIBE configuration hash
- wallclock timestamp (UTC)

This is enforced at the runner level — see CONTRACTS.md §9.

## Privacy

- No accounts, logins, or stored user history — the only input is a YouTube
  URL and a start/end time.
- The live path runs in the cloud: the HuggingFace Space fetches **only** the
  selected `[startSec, endSec)` segment, runs TRIBE, and returns the small
  per-region result. The downloaded segment lives in a temporary working
  directory that is deleted when the job finishes; only the derived activation
  JSON is persisted (S3 + DynamoDB) so the browser can poll for it.
- The app embeds no analytics SDKs or telemetry. If a worker is tempted to add
  one, open an issue instead.
- The precomputed gallery ships no user data at all — it is static JSON baked
  offline from a fixed set of example clips.
