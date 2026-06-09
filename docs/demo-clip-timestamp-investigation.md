# Investigation: gallery brain `timestamps` overshoot the 90 s segment

**TL;DR — it's a real time-axis (stride) bug in the *real-bake* path, not a
benign model-time/video-time distinction, and not in `build_demo_gallery.py`'s
mock path.** The baked brains' `timestamps` are built at a hardcoded **1.5 Hz**
that does **not** match the number of timepoints TRIBE actually emits, so the
axis runs to ~124–140 s for a 90 s segment. For the new "video beside the
brain" slider the brain axis must be the video's `[0, durationSec]`, so this
should be fixed bake-side and re-baked (not done here, per the brief).

## What I observed

Every committed `apps/web/public/demo-predictions/*.json` (real bake,
`modelVersion` = the TRIBE sha) has `durationSec = 90`, a uniform stride of
**0.667 s = 1/1.5**, and a timepoint count `T` that varies per clip:

| slug | durationSec | T | timestamps span | stride |
|---|---|---|---|---|
| nasa-pillars-of-creation-3d | 90 | 187 | 0 … 124.0 | 0.667 |
| npr-tiny-desk-yo-yo-ma | 90 | 192 | 0 … 127.33 | 0.667 |
| national-portrait-gallery-… | 90 | 210 | 0 … 139.33 | 0.667 |
| ted-ed-why-is-english-so-confusing | 90 | 210 | 0 … 139.33 | 0.667 |
| ted-how-language-shapes-thought | 90 | 210 | 0 … 139.33 | 0.667 |
| bbc-earth-nature-in-4k | 90 | 211 | 0 … 140.0 | 0.667 |
| pbs-newshour-ai-science | 90 | 211 | 0 … 140.0 | 0.667 |
| red-bull-fpv-drone-mountain-biking | 90 | 211 | 0 … 140.0 | 0.667 |

At 1.5 Hz a 90 s clip would be ~135 timepoints; the bakes have 187–211. So the
**axis is ~38–55 % too long**, and (because `T` varies) there is no single Hz
that maps all clips back to 90 s.

## Root cause

The bake fetches **exactly** the `[startSec, endSec)` = 90 s window —
`neural_media_pipeline.downloader._yt_dlp_fetch` uses
`download_ranges=[(start, end)]` (the API form of
`--download-sections "*start-end"`). So TRIBE's input is ~90 s of video, and it
emits `T` timepoints for it (187–211 here — the count tracks the source's
frame/feature sampling, so nominally-90 s clips differ).

Then `scripts/predict_one_url.py::_build_timestamps(T)` labels them at a
**hardcoded 1.5 Hz**:

```python
SAMPLE_RATE_HZ = 1.5
dt = 1.0 / SAMPLE_RATE_HZ
return [round(i * dt, WIRE_TIMESTAMP_DECIMALS) for i in range(num_timepoints)]
```

For the **real** `TribeBackend`, `duration_s`/`sample_rate_hz` are ignored —
`T` comes from the model — so `timestamps[-1] = (T-1)/1.5` runs to 124–140 s
regardless of the true 90 s span. (The 1.5 Hz constant is a leftover assumption;
TRIBE's effective output rate for these clips is ~2.08–2.34 Hz, and it isn't
even constant clip-to-clip.)

`build_demo_gallery.py`'s **mock** path is already correct — it derives the
axis from the clip length (`step = duration / (series_len - 1)`), spanning
`[0, duration]`. Only the **real-bake** path (`predict_one_url.py`, and the
deployed Space `services/hf-space/app.py::_build_timestamps`, which has the
identical hardcoded-1.5 Hz line) mislabels.

## Empirical confirmation

`scripts/build_demo_clips.py` fetches the same window and trims to exactly
`[startSec, endSec]`. The produced clips are ~90 s each (see run summary),
confirming the segment really is 90 s — so the activations cover 90 s and the
124–140 s axis is purely a labeling error, not extra content.

Concretely, for `nasa-pillars-of-creation-3d` (`[20, 110)`): yt-dlp returned a
112 s head (`[0, endSec+2]`) and the trimmed clip is **90.0 s** exactly, while
its baked brain has `T=187` timestamps running to **124 s**. 90 s of video,
187 timepoints → ~2.08 Hz, not the 1.5 Hz the axis assumes. The other seven
clips trim to 90.0 s the same way.

## Is it "model-time vs video-time" (benign)?

No. The current axis is **neither**: it's not video-time (the segment is 90 s),
and it's not true model-time either (1.5 Hz is the wrong constant — and `T`
varies, so no fixed rate fits). For the "one slider drives video + brain"
feature the brain series must be indexed in the **video's** seconds,
`[0, durationSec]`.

## Proposed fix (bake-side; clean; NOT applied / NOT re-baked here)

Make the real path derive the axis from the analyzed segment duration, exactly
like the mock path already does — in `scripts/predict_one_url.py`:

```python
def _build_timestamps(num_timepoints: int, duration_s: float) -> list[float]:
    if num_timepoints <= 0:
        return []
    if num_timepoints == 1:
        return [0.0]
    step = duration_s / (num_timepoints - 1)
    return [round(i * step, WIRE_TIMESTAMP_DECIMALS) for i in range(num_timepoints)]
```

…called as `_build_timestamps(int(activations.shape[0]), duration_s)`
(`duration_s` is already in scope in `_run_pipeline`). Apply the same change to
`services/hf-space/app.py::_build_timestamps` (pass `endSec - startSec`) so the
deployed Space and the local bake agree. Then re-bake the gallery
(`python scripts/build_demo_gallery.py`) and redeploy the Space.

Result: every brain's `timestamps` span `[0, durationSec]` (≈ `[0, 90]`),
matching `videoDurationSec` and the self-hosted clip, so the slider stays in
sync.

**Interim option for the frontend** (other workers' call): until a re-bake
lands, normalize the brain axis on read — `t_video = t / timestamps[-1] *
durationSec` — which maps the existing `[0, 124…140]` onto `[0, durationSec]`.
The `len(timestamps) == len(byRegion[region])` invariant is unaffected either
way (only the values change, not the count).

## Scope note

Per the brief this is **report-only**: I did not modify `predict_one_url.py`,
`app.py`, `build_demo_gallery.py`, or the `demo-predictions/` JSON, and did not
re-bake. Handing the fix to the brain.
