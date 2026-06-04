// Tiny in-process mock of the AWS /v2/jobs* API contract used by the e2e
// suite. Runs on :3001. The web app is started with
// NEXT_PUBLIC_API_BASE_V2=http://localhost:3001 so all job calls land here.
//
// Status progression (per API contract):
//   pending → downloading (1s) → inferring (3s) → done (6s)
//
// Magic strings in the submitted URL trigger non-happy paths:
//   "BLOCK_ME"    → pending → downloading → failed_download (2s)
//                   (used to exercise the upload-fallback CTA)
//   "LONG_VIDEO"  → rejected_duration (2s)

import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { randomUUID } from "node:crypto";

type JobStatus =
  | "pending"
  | "downloading"
  | "inferring"
  | "done"
  | "failed_download"
  | "failed_inference"
  | "rejected_duration";

interface Job {
  jobId: string;
  status: JobStatus;
  createdAt: number;
  url?: string;
  uploadKey?: string;
  resultUrl?: string;
  error?: string;
  timers: NodeJS.Timeout[];
}

const PORT = Number(process.env.MOCK_PORT ?? 3001);

// Must match shared/types.ts REGION_IDS exactly — the client lib validates
// every region is present with a number[] of the same length as timestamps.
const REGION_IDS = [
  "v1",
  "v2",
  "v3",
  "v4",
  "auditory",
  "language",
  "ffa",
  "vwfa",
] as const;

const TIMESTAMPS = [0, 0.5, 1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5];
const STUB_SERIES = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1];

const STUB_ACTIVATION = {
  videoDurationSec: 12.5,
  timestamps: TIMESTAMPS,
  byRegion: Object.fromEntries(REGION_IDS.map((r) => [r, [...STUB_SERIES]])),
  modelVersion: "mock-1",
};

const jobs = new Map<string, Job>();

function clearJobTimers(job: Job) {
  for (const t of job.timers) clearTimeout(t);
  job.timers = [];
}

function scheduleHappyPath(job: Job) {
  clearJobTimers(job);
  job.timers.push(
    setTimeout(() => {
      job.status = "downloading";
    }, 1000),
  );
  job.timers.push(
    setTimeout(() => {
      job.status = "inferring";
    }, 3000),
  );
  job.timers.push(
    setTimeout(() => {
      job.status = "done";
      job.resultUrl = `http://localhost:${PORT}/mock-results/${job.jobId}.json.gz`;
    }, 6000),
  );
}

function scheduleBlocked(job: Job) {
  clearJobTimers(job);
  job.timers.push(
    setTimeout(() => {
      job.status = "downloading";
    }, 500),
  );
  job.timers.push(
    setTimeout(() => {
      job.status = "failed_download";
      job.error = "download_blocked";
    }, 2000),
  );
}

function scheduleRejected(job: Job) {
  clearJobTimers(job);
  job.timers.push(
    setTimeout(() => {
      job.status = "rejected_duration";
      job.error = "video_too_long";
    }, 2000),
  );
}

function snapshot(job: Job) {
  return {
    jobId: job.jobId,
    status: job.status,
    createdAt: job.createdAt,
    elapsedSec: (Date.now() - job.createdAt) / 1000,
    resultUrl: job.resultUrl,
    error: job.error,
  };
}

function applyCors(res: ServerResponse) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET,POST,PUT,OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "*");
}

function send(res: ServerResponse, status: number, body: unknown) {
  applyCors(res);
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json");
  res.end(JSON.stringify(body));
}

async function readJson(req: IncomingMessage): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  for await (const c of req) chunks.push(c as Buffer);
  if (chunks.length === 0) return {};
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    return {};
  }
}

function newJob(): Job {
  return {
    jobId: randomUUID(),
    status: "pending",
    createdAt: Date.now(),
    timers: [],
  };
}

function progressionFor(url: string | undefined, job: Job) {
  if (url && url.includes("LONG_VIDEO")) scheduleRejected(job);
  else if (url && url.includes("BLOCK_ME")) scheduleBlocked(job);
  else scheduleHappyPath(job);
}

const server = createServer(async (req, res) => {
  const url = req.url ?? "";
  const method = req.method ?? "GET";

  if (method === "OPTIONS") {
    applyCors(res);
    res.statusCode = 204;
    res.end();
    return;
  }

  // POST /v2/jobs
  if (method === "POST" && url === "/v2/jobs") {
    const body = await readJson(req);
    const job = newJob();
    job.url = typeof body.url === "string" ? body.url : undefined;
    jobs.set(job.jobId, job);
    progressionFor(job.url, job);
    send(res, 201, { jobId: job.jobId });
    return;
  }

  // POST /v2/jobs/upload
  if (method === "POST" && url === "/v2/jobs/upload") {
    const job = newJob();
    const uploadKey = `uploads/${job.jobId}.mp4`;
    job.uploadKey = uploadKey;
    jobs.set(job.jobId, job);
    send(res, 201, {
      jobId: job.jobId,
      uploadUrl: `http://localhost:${PORT}/mock-s3/${uploadKey}`,
      uploadKey,
    });
    return;
  }

  // PUT /mock-s3/* — accepts the binary upload, body discarded
  if (method === "PUT" && url.startsWith("/mock-s3/")) {
    req.on("data", () => {});
    req.on("end", () => {
      applyCors(res);
      res.statusCode = 200;
      res.end();
    });
    return;
  }

  // POST /v2/jobs/upload/{id}/confirm
  const confirmMatch = /^\/v2\/jobs\/upload\/([^/]+)\/confirm$/.exec(url);
  if (method === "POST" && confirmMatch) {
    const id = confirmMatch[1];
    const job = jobs.get(id);
    if (!job) {
      send(res, 404, { error: "not_found" });
      return;
    }
    scheduleHappyPath(job);
    send(res, 200, { jobId: id });
    return;
  }

  // GET /v2/jobs/{id}
  const getMatch = /^\/v2\/jobs\/([^/]+)$/.exec(url);
  if (method === "GET" && getMatch) {
    const id = getMatch[1];
    const job = jobs.get(id);
    if (!job) {
      send(res, 404, { error: "not_found" });
      return;
    }
    send(res, 200, snapshot(job));
    return;
  }

  // GET /mock-results/{id}.json.gz — return the stub activation as plain JSON
  // (mock; not gzip'd — fine for tests that just check the URL is reachable).
  if (method === "GET" && url.startsWith("/mock-results/")) {
    send(res, 200, STUB_ACTIVATION);
    return;
  }

  // GET /__health — readiness probe used by Playwright's webServer wait.
  if (method === "GET" && url === "/__health") {
    send(res, 200, { ok: true });
    return;
  }

  send(res, 404, { error: "not_found", url, method });
});

server.listen(PORT, () => {
  // eslint-disable-next-line no-console
  console.log(`[mock-server] listening on http://localhost:${PORT}`);
});

const shutdown = () => {
  for (const job of jobs.values()) clearJobTimers(job);
  server.close(() => process.exit(0));
};
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
