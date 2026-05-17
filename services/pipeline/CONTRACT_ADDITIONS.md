# Contract additions requested — round 3

The api-orchestrator worker is adding `POST /api/v1/import` and
`GET /api/v1/import/{job_id}` in the same round. The shapes below need
to land in `shared/CONTRACTS.md` (and mirrored to `shared/schemas.py` +
`shared/types.ts`). I am NOT editing `shared/` directly — terminal 1
will land these.

The data-pipeline worker (this package) emits `ProgressEvent` from
`Orchestrator.run(progress=...)`. The api worker stores the latest event
per job and serves it back via the polling endpoint. The shapes below
are designed to be 1:1 with what the orchestrator already produces.

---

## 1. `ImportJob`

The polling response from `GET /api/v1/import/{job_id}`.

| Field              | Type                   | Notes                                            |
|--------------------|------------------------|--------------------------------------------------|
| `id`               | string (UUID)          | Job id, assigned by the api on POST              |
| `status`           | `ImportJobStatus`      | See enum below                                   |
| `phase`            | `ImportPhase` \| null  | Most recent `ProgressEvent.phase`; null until first event |
| `videos_total`     | integer                | Mirrors `ProgressEvent.videos_total` (0 until parsing done) |
| `videos_processed` | integer                | Mirrors `ProgressEvent.videos_processed`         |
| `message`          | string \| null         | Most recent `ProgressEvent.message` (video id, never URL) |
| `created_at`       | string (ISO-8601 UTC)  | When the job was POSTed                          |
| `updated_at`       | string (ISO-8601 UTC)  | When the api last received a progress event      |
| `completed`        | integer                | `IngestSummary.completed`, populated on terminal status |
| `failed`           | integer                | `IngestSummary.failed`, populated on terminal status    |
| `error`            | string \| null         | Set only when `status == "failed"` for a fatal job-level error |

## 2. `ImportJobStatus` (string enum)

| Value      | Meaning                                                          |
|------------|------------------------------------------------------------------|
| `queued`   | POST returned; the background thread hasn't picked the job up yet |
| `running`  | At least one `ProgressEvent` received; pipeline is in progress    |
| `complete` | The orchestrator returned its `IngestSummary` with `failed == 0`  |
| `partial`  | Orchestrator returned with both `completed > 0` and `failed > 0`  |
| `failed`   | The orchestrator raised before completing (e.g. unparseable export) |

Note: per-video failures land in `IngestSummary.errors` and are reflected
in `failed`/`completed`. The job itself is only `failed` if the whole
ingest aborted.

## 3. `ImportPhase` (string enum)

Mirrors `neural_media_pipeline.orchestrate.Phase` exactly:

```
"parsing" | "downloading" | "preprocessing" | "inferring"
```

## 4. `POST /api/v1/import` request shape

The pipeline accepts a TikTok export as either:

* a `user_data.json` file, or
* a `.zip` archive containing `user_data.json` at any depth (the
  importer reads it in memory; nothing is extracted to disk).

The api worker should treat both as opaque uploads and pass the saved
path straight to `Orchestrator.run`.

## 5. Privacy invariants the api must preserve

`ProgressEvent.message` carries the stable video id (a UUID5 of the
source URL) and **never** the URL itself. The api worker should not
add the source URL to `ImportJob.message` or to any logging on the
import path; doing so would violate `docs/scientific-framing.md`.

---

## Operational notes (NOT contract — for terminal 1 + api worker awareness)

* **Mock-mode timing.** With `skip_download=skip_preprocess=True`, the
  bottleneck is the inference runner's `np.savez_compressed` call (lives
  in `services/inference/neural_media_inference/runner.py`). Measured
  ~125 ms per video on Apple Silicon; a 200-video export takes ~25 s
  end-to-end. The 5-second goal from the round-3 brief is not reachable
  without a faster activation persistence path. The polling endpoint
  was designed to handle multi-second jobs, so this is not a blocker —
  but the integration lead may want to plumb a `--no-compress`
  flag into the inference runner.
* **`OrchestratorConfig.backend`.** New optional field that forwards to
  `run_inference(backend=...)`. The api can pass `MockBackend()`
  explicitly if it wants to lock the backend even after a real TRIBE
  backend becomes available.
* **`OrchestratorConfig.data_root`.** Convenience field that expands to
  the four `*_dir` / `db_path` defaults. Lets the api worker say
  `OrchestratorConfig(data_root=cfg.data_root, skip_download=True, ...)`
  without enumerating four paths.
