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
