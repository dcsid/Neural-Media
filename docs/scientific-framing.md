# Scientific framing — what Neural Media does and does not measure

This document is binding for every worker. Copy that holds up to scrutiny is
a product feature, not a marketing one. If something here changes, it
changes in this file first.

## What the tool DOES measure

- **Predicted BOLD fMRI response** (cortical surface, 20,484 vertices) for
  each watched video, from the **average** of TRIBE v2's 720 training subjects.
- Predicted engagement in well-localized cortical regions: visual cortex,
  auditory cortex, language network, face-selective regions.
- Divergence and similarity **between videos** — clustering content by
  predicted neural fingerprint.
- Aggregate patterns over a user's watch history: time-of-day shifts,
  content-type breakdowns, session-level variation.

## What it does NOT measure (do not claim these)

- **Individual brain response.** The model predicts an average, not the
  specific user's brain.
- **Subcortical reward / "addiction" circuitry.** Nucleus accumbens, VTA,
  etc. are not in the output space.
- **Cumulative effects, habituation, or neuroplasticity** over time.
- **Subjective states**: aesthetic preference, confusion, cognitive
  overload, memory encoding, satisfaction.
- **"How TikTok is rewiring your brain"** or any rhetoric in that family.

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

The app ships two inference backends and a `mode` toggle on `/import`:

- **`real`** — actually runs TRIBE v2 on the downloaded `.mp4`. Predictions
  are grounded in the video's frames + audio.
- **`mock`** (default in the UI) — runs `MockBackend`
  (`services/inference/neural_media_inference/backend.py:53`), which is
  **a sine wave per cortical vertex seeded by `SHA-256(video_id, seed)`.**

In mock mode:

- The video is never downloaded. yt-dlp is not invoked.
- The video file is never opened, decoded, or read. Frames and audio
  are not examined.
- The output is a deterministic function of the URL *string*. Two URLs
  pointing to wildly different videos produce different mock activations
  only because the URL strings differ — not because the content differs.
- Feeding the same URL twice produces byte-identical activations,
  regardless of whether the underlying video ever existed.

This is intentional: mock mode exercises the full pipeline plumbing
(parse → schedule → aggregate → API → UI) without yt-dlp or GPU. It is
the right tool for testing, demos of the *system architecture*, and any
case where the integrity of the prediction does not matter.

It is **not** a prediction about the user's videos. Specifically:

- Mock-mode region bars and brain-mesh colors are honest aggregations
  of dishonest inputs. The reduction math is correct; the vertex values
  being reduced are URL-hash sine waves.
- The reproducibility envelope (CONTRACTS.md §9) still records
  everything correctly — `model_id="tribe-v2-mock"` is the truth signal.
  Any consumer that shows mock outputs as if they were real predictions
  is mis-labeling.

**UI requirement:** wherever the dashboard or any other view renders
data backed by an `InferenceRun` with `model_id` beginning `tribe-v2-mock`,
the user must see a visible label saying so. The frontend-dashboard
worker owns this label; do not strip it.

## Reproducibility envelope

Every `InferenceRun` MUST log:

- model id and version (e.g. `tribe-v2 / 2024-09-rev3`)
- random seed
- preprocessing params (resolution, fps, audio sample rate)
- TRIBE configuration hash
- wallclock timestamp (UTC)

This is enforced at the runner level — see CONTRACTS.md §8.

## Privacy

- The pipeline runs entirely on-device.
- Watch history never leaves the machine.
- The app does not embed analytics SDKs or telemetry. If a worker is
  tempted to add one, instead open an issue.
- The downloaded videos directory (`data/videos/`) is `.gitignore`d. Do
  not commit user data.
