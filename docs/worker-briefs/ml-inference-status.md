# ml-inference — real-mode status

**As of 2026-05-17.** This file is the ground truth on whether the
`TribeBackend` path can actually run today. The integration lead should
fold the user-facing message into the Dashboard before claiming "real
mode supported".

## TL;DR

| Question                                          | Answer                                  |
|---------------------------------------------------|-----------------------------------------|
| Is `facebook/tribev2` downloadable from HF?       | **Yes** — public, `gated:false`, CC-BY-NC-4.0. |
| Is `tribev2` installable from PyPI?               | **No** — not published.                 |
| Is `tribev2` installable from source?             | **Yes** — `pip install git+https://github.com/facebookresearch/tribev2`. Already wired into `services/inference/pyproject.toml::optional-dependencies.real`. |
| Can a user run real mode on a 2021 MacBook M1?    | **No** — needs an NVIDIA GPU box (~10–20 GB VRAM for the full forward path with LLaMA-3.2 / V-JEPA2 backbones). CPU inference is theoretically possible but not practical for any demo length. |
| Will the existing `TribeBackend` wrapper work?    | **Probably yes** — see the signature audit below. **One assumption is unverified**: the cortical-vertex ordering on the output is the first 20,484 indices in lh-then-rh order. This must be confirmed against a real run before downstream region masks are trusted. |

## Recommended user-facing UI string

Until a real-mode smoke has been run on a real GPU and the vertex
ordering / output range have been confirmed against ground truth:

> Real mode requires a GPU host and is not yet enabled. The dashboard is
> running with `tribe-v2-mock` — synthesized, deterministic activations
> that exercise the data path end-to-end. See
> `docs/worker-briefs/ml-inference-status.md` for what shipping real
> mode would require.

## Evidence

### 1. HuggingFace `facebook/tribev2`

```
$ curl -sI https://huggingface.co/facebook/tribev2 | head -5
HTTP/2 200
content-type: text/html; charset=utf-8
content-length: 135045
date: Sun, 17 May 2026 20:56:54 GMT
etag: W/"20f85-kjZfRoNOzc0h+Wwm2865uWnS8bo"
```

```
$ curl -sS https://huggingface.co/api/models/facebook/tribev2 | jq '...'
{
  "id": "facebook/tribev2",
  "private": false,
  "gated": false,
  "disabled": false,
  "downloads": 183000,
  "likes": 522,
  "sha": "f894e783020944dcd96e5568550afe2aa9743f9f",
  "lastModified": "2026-03-27T09:07:48Z",
  "siblings": [".gitattributes", "LICENSE", "README.md",
               "best.ckpt", "config.yaml"],
  "cardData": {"license": "cc-by-nc-4.0"}
}
```

**Caveat**: this is not a standard `transformers` repo — `config.json`
returns 404. The checkpoint is loaded by the custom `tribev2` Python
package via `TribeModel.from_pretrained(...)`, not
`transformers.AutoModel.from_pretrained(...)`. `backend_tribe.py`
already imports `from tribev2 import TribeModel`, which is correct.

### 2. PyPI `tribev2`

```
$ pip index versions tribev2
ERROR: No matching distribution found for tribev2

$ pip install --dry-run tribev2
ERROR: Could not find a version that satisfies the requirement tribev2
       (from versions: none)
ERROR: No matching distribution found for tribev2
```

PyPI doesn't know about `tribev2`. This is **not** a bug in our
pyproject — `services/inference/pyproject.toml::[real]` correctly
declares it as a VCS dependency:

```toml
"tribev2 @ git+https://github.com/facebookresearch/tribev2"
```

so `pip install -e '.[real]'` does resolve, just from GitHub.

### 3. Upstream source repo

```
$ curl -sI https://github.com/facebookresearch/tribev2 | head -1
HTTP/2 200

$ curl -sS https://api.github.com/repos/facebookresearch/tribev2
  full_name:           facebookresearch/tribev2
  description:         "code to train and evaluate TRIBE v2,
                        a multimodal model for brain response prediction"
  default_branch:      main
  pushed_at:           2026-05-11T10:17:23Z
  stargazers_count:    2621
```

The README confirms the contract `backend_tribe.py` assumes:

```python
from tribev2 import TribeModel
model = TribeModel.from_pretrained("facebook/tribev2", cache_folder="./cache")
df = model.get_events_dataframe(video_path="path/to/video.mp4")
preds, segments = model.predict(events=df)
print(preds.shape)  # (n_timesteps, n_vertices) on fsaverage5 (~20k)
```

(Source:
[github.com/facebookresearch/tribev2/blob/main/README.md](https://github.com/facebookresearch/tribev2/blob/main/README.md).)

## What real-mode setup actually requires

Cannot be run on the user's macOS dev machine. Realistic options:

1. **Cloud GPU box (recommended)** — A single L40 / A100 / H100 with at
   least 24 GB VRAM. RunPod / Lambda Labs / Modal / Fly GPUs all work.
   Storage: ~30 GB for the HF cache (TRIBE pulls LLaMA-3.2, V-JEPA2, and
   Wav2Vec-BERT weights transitively on first use). Provision a Linux
   box with CUDA 12.x and Python 3.11–3.13 (the `torch>=2.5.1,<2.7` pin
   does not yet have 3.14 wheels at the time of writing).
2. **Modal or RunPod serverless function** — set up an endpoint that
   wraps `run_inference(..., backend=TribeBackend(...))` and is invoked
   by the api-orchestrator over HTTP. Decouples GPU rental from the
   user's laptop. Requires solving a video-upload path; data-pipeline
   downloads videos to `data/videos/` on the local box today.
3. **Owned hardware** — RTX 4090 / 5090 in a desktop box would work,
   same software stack as option 1.

The wrapper itself is local-first; only the model execution needs to
move. The local data-pipeline can keep running on macOS, downloading
videos and posting them to the remote endpoint.

## `TribeBackend` wrapper audit

`services/inference/neural_media_inference/backend_tribe.py` —
assumption-by-assumption against the upstream README and source:

| Wrapper line | Assumption                                  | Verified? |
|--------------|---------------------------------------------|-----------|
| 47           | repo id `"facebook/tribev2"`                | ✓ exists on HF |
| 63           | `from tribev2 import TribeModel`            | ✓ matches `tribev2/__init__.py` re-export of `demo_utils.TribeModel` |
| 128          | `HfApi().repo_info(...).sha` resolves a 40-char sha | ✓ HF API returns `sha=f894e7…` for `revision="main"` (2026-03-27 snapshot) |
| 133–137      | `TribeModel.from_pretrained(repo_id, revision=sha, cache_folder=...)` then `.to(device).eval()` | ✓ matches `demo_utils.TribeModel.from_pretrained` signature |
| 194–195      | `model.get_events_dataframe(video_path=str(path))` then `model.predict(events=events)` returning `(preds, segments)` | ✓ matches README quick-start verbatim |
| 199–201      | `preds` may be `torch.Tensor` or `np.ndarray`; normalize via `.detach().to("cpu").numpy()` | ✓ defensive; safe either way |
| 203–207      | `preds.ndim == 2`, `preds.shape[1] >= 20484` | ✓ README says `(n_timesteps, n_vertices)` on fsaverage5 (~20k) |
| 210          | First 20484 columns are cortical vertices in **lh[0:10242] then rh[10242:20484]** order | **⚠ NOT VERIFIED.** README says fsaverage5 but does not commit to hemisphere ordering. `region_masks.json` was built assuming lh-then-rh; if TRIBE emits rh-then-lh or interleaved, region masks index the wrong vertices. **Must confirm against a real-mode smoke before any external demo.** |
| 213–215      | Sigmoid clamps `[-30, 30]` z → `(0, 1)`     | ✓ numerically sound; see §"Sigmoid audit" below. README says "predicts fMRI responses" but does not state whether the model head emits raw z-scores. **Inspect a single forward pass output range against the model card before assuming z-scoring is needed.** |
| 165          | Video file resolved as `{videos_dir}/{video_id}.{mp4|webm|mkv|mov}` | Upstream's `VALID_SUFFIXES` for `video_path` is `{.mp4, .avi, .mkv, .mov, .webm}` — we omit `.avi` (minor; data-pipeline emits `.mp4` per `DEFAULT_PREPROCESSING_PARAMS`) |
| 110          | `accept_licenses=True` gate                  | ✓ documented; matches the CC-BY-NC + LLaMA-3.2 transitive licensing reality |

The transitive license stack on first use of `TribeModel.from_pretrained`:

- TRIBE v2 weights — CC-BY-NC-4.0 (Meta)
- LLaMA-3.2 — Llama 3.2 Community License (Meta, non-commercial OK with attribution)
- V-JEPA2 — CC-BY-NC-4.0 (Meta)
- Wav2Vec-BERT — Apache 2.0 (Meta)

All non-commercial OK; matches Neural Media's CC-BY-NC-4.0 policy.

## Sigmoid audit (`backend_tribe.py:212-215`)

The wrapper applies `out = 1 / (1 + exp(-cortex))` after clipping
`cortex` into `[-30, 30]`. Numerical evidence:

```
z       sigmoid
-5.00   0.006693
-3.00   0.047426
-1.00   0.268941
+0.00   0.500000
+1.00   0.731059
+3.00   0.952574
+5.00   0.993307
```

- **Strictly monotonic.** `np.diff(sigmoid(linspace(-5, 5, 21)))` is
  positive everywhere, minimum step `4.29e-3`. The order of any pair
  of vertices, regions, or videos is preserved end-to-end through the
  transform.
- **Bounded.** Sigmoid of the guard-clipped `[-30, 30]` range maps to
  `(~9.4e-14, 1 - 9.4e-14)` — comfortably inside float32 `[0, 1]` so
  the runner's range assertion (`runner.py:120`) cannot be tripped by
  this code path. The `clip(-30, 30)` is precautionary; even without
  it sigmoid is well-defined.
- **Sign information lost.** A z = -2 (BOLD *suppression* — below
  baseline) sigmoid → 0.119; a z = +2 (BOLD activation) sigmoid →
  0.881. Both end up positive in `[0, 1]`. Anything downstream that
  wanted to display "suppression" as a different visual quality than
  "activation" cannot recover that from our output. The cividis LUT
  treats this as a single dark-to-bright axis, which is consistent with
  the framing in `docs/scientific-framing.md` ("comparative claims
  only — `video A predicts higher V1 than video B`"). Mean activation
  reported as a number is, after sigmoid, the mean of a `[0, 1]`
  signal — it is no longer a z-score. This is a deliberate trade
  documented below as a scientific-framing addendum.

### Suggested addendum for `docs/scientific-framing.md`

Drop this paragraph in under "Rules for copy and UI text":

> **Predicted activation is a relative, bounded number.** Real-mode
> TRIBE v2 emits z-scored BOLD signal in roughly `[-3, 3]`. Neural
> Media squashes that through a logistic sigmoid into `[0, 1]` before
> any aggregation, so all `mean` / `peak` / `sustained` values in the
> UI live on a 0-to-1 scale, not in z-space. The sigmoid is strictly
> monotonic, so within-video and between-video orderings are preserved
> — *V1 higher in video A than video B* remains true after the
> transform — but BOLD *suppression* (z<0) and *activation* (z>0) both
> map to positive numbers and become visually indistinguishable.
> Magnitude differences are deliberately compressed: a vertex at z=+1
> and another at z=+3 read as 0.73 vs 0.95 on the UI, not 1× vs 3×.
> Compare videos with each other, not the numbers against a clinical
> threshold.

## `MockBackend` vs `TribeBackend` value-range alignment

`backend.py:53` `MockBackend.infer` produces:

```
shape:     (90, 20484)       (for duration_s=60, sample_rate_hz=1.5)
dtype:     float32
min:       0.000209
max:       1.000000
mean:      0.324086
std:       0.221760
in [0,1]:  True
==0.0 count: 0 / 1,843,560   (open at the low end)
==1.0 count: 1,421 / 1,843,560   (~0.08% saturate; closed via np.clip)
```

`TribeBackend.infer` post-sigmoid (simulated against z ~ N(0,1)):

```
min:       0.004725
max:       0.993295
mean:      0.500159
std:       0.208225
==0.0:     0
==1.0:     0
```

**Both lie inside `[0, 1]` float32**, so the runner's range assertion
holds for both and downstream code (aggregation, brain-viz LUT) does
not need a special case. Differences worth knowing about:

- The mock can hit `1.0` exactly (`np.clip(..., 0.0, 1.0)`); sigmoid
  cannot. This is a single-ULP difference, not a contract violation.
- Mock mean ≈ 0.32, real-via-sigmoid mean ≈ 0.50. The UI's
  "mean activation: 0.32" today looks dimmer than real-mode numbers
  will. Anything calibrated against mock values (e.g., the LUT's
  midpoint, region-bar widths normalised to seen ranges) will look
  different when real mode lands. Visual layout is not affected
  because cividis is linear in `[0, 1]`.

Recommendation: leave both as-is. The two stay byte-compatible at the
contract level; the *distributional* drift is intentional and is
exactly what an audience would expect ("the real model produces more
variation"). Document the mean-shift in
`docs/scientific-framing.md` so a careful reader doesn't read the
mock-mode dashboard as evidence of low engagement.

## Open items for the next time someone has GPU access

1. **Hemisphere ordering on TRIBE output.** Run a forward pass, dump
   the first and last 10 vertex columns, and confirm the lh-then-rh
   layout that `region_masks.json` assumes. If wrong, the masks need a
   permutation map — the data-pipeline aggregator and the brain-viz
   `fsaverage5.regions.bin` are byte-aligned to the current ordering
   so both flip together.
2. **Raw-output range.** Inspect `preds.min(), preds.max(), preds.mean()`
   on a 30-second clip before sigmoid. If TRIBE emits values already
   in `[0, 1]` (e.g., the model card mentions a sigmoid head) the
   wrapper's sigmoid is a double-application; if values are in
   `[-3, 3]` z-space as assumed, leave the transform.
3. **Determinism.** `torch.manual_seed(seed)` in
   `backend_tribe.py:188` is a best-effort gate. Verify two runs with
   the same `(video_id, seed)` produce byte-identical activations on
   the target GPU; if not, document residual non-determinism in the
   reproducibility envelope.
