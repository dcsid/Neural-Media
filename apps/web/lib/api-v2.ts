// Client-side bindings for the v2 "single video → brain" job API.
//
// The backend (T2/T3) implements:
//   POST /v2/jobs                          { url }                          → 201 { jobId }
//   POST /v2/jobs/upload                   { filename, contentType }        → 201 { jobId, uploadUrl, uploadKey }
//   POST /v2/jobs/upload/{id}/confirm                                       → 200
//   GET  /v2/jobs/{id}                                                      → 200 JobStatus
//
// The result file at `resultUrl` is the Activation JSON, served gzip'd
// from S3 with Content-Encoding: gzip — `fetch().json()` decodes it
// transparently in the browser.

import { REGION_IDS, type RegionId } from "@shared/types";

// ---------------------------------------------------------------------------
// Status enum + discriminated job-state union
// ---------------------------------------------------------------------------

export const JOB_STATUSES = [
  "pending",
  "downloading",
  "inferring",
  "done",
  "failed_download",
  "failed_inference",
  "rejected_duration",
] as const;

export type JobStatus = (typeof JOB_STATUSES)[number];

export type TerminalSuccessStatus = "done";
export type TerminalFailureStatus =
  | "failed_download"
  | "failed_inference"
  | "rejected_duration";
export type InFlightStatus = "pending" | "downloading" | "inferring";

export function isTerminalStatus(s: JobStatus): boolean {
  return s === "done" || isFailureStatus(s);
}

export function isFailureStatus(s: JobStatus): s is TerminalFailureStatus {
  return (
    s === "failed_download" ||
    s === "failed_inference" ||
    s === "rejected_duration"
  );
}

// Common envelope returned by GET /v2/jobs/{id}. Optional fields fill in
// depending on status — `resultUrl` only on done, `error` only on failures.
//
// Field shapes must stay in lockstep with what jobs_status Lambda emits
// (infra/aws/lambdas/jobs_status/handler.py). Source of truth is the
// Lambda — this is the TypeScript projection of that JSON body.
export interface JobStatusResponse {
  jobId: string;
  status: JobStatus;
  // Epoch seconds, as written by jobs_create when the job row was
  // inserted (see lambdas/jobs_status/handler.py). The /single page
  // does not use this field for display — it tracks elapsed time
  // against its own startedAtMs wall clock — but downstream consumers
  // can convert with `new Date(createdAt * 1000)` (seconds → ms).
  createdAt: number;
  // jobs_status returns null when createdAt is missing/zero, otherwise
  // a non-negative integer. The page guards with `?? 0` so a null
  // doesn't propagate into the elapsed-time display.
  elapsedSec: number | null;
  resultUrl?: string;
  // jobs_status echoes this from the DDB row when the HF Space sent
  // it on the done callback. Diagnostic only — not all responses
  // include it (failure/intermediate statuses won't).
  modelVersion?: string;
  // Short machine-readable error code. The frontend special-cases
  // "tiktok_blocked" on failed_download to surface the upload fallback.
  error?: string;
  // Actual decoded video duration as measured by ffprobe on the HF Space.
  // Reported through the hf-callback once inference completes; absent on
  // pre-callback statuses.
  durationSec?: number;
}

export interface CreateUrlJobResponse {
  jobId: string;
}

export interface CreateUploadJobResponse {
  jobId: string;
  uploadUrl: string;
  uploadKey: string;
}

// ---------------------------------------------------------------------------
// Activation JSON (the contents of resultUrl)
// ---------------------------------------------------------------------------

// byRegion maps each REGION_ID to a same-length array of activation values,
// one per entry in `timestamps`. The BrainMesh component's keyframeVertices
// prop has the exact same shape (Record<string, number[]>) — pass it
// through directly.
export interface ActivationPayload {
  videoDurationSec: number;
  timestamps: number[];
  byRegion: Record<RegionId, number[]>;
  modelVersion: string;
}

// ---------------------------------------------------------------------------
// Error type
// ---------------------------------------------------------------------------

export class ApiV2Error extends Error {
  readonly kind: "offline" | "http" | "parse" | "validation";
  readonly status?: number;
  readonly url: string;
  readonly body?: unknown;

  constructor(args: {
    kind: "offline" | "http" | "parse" | "validation";
    url: string;
    message: string;
    status?: number;
    body?: unknown;
  }) {
    super(args.message);
    this.name = "ApiV2Error";
    this.kind = args.kind;
    this.url = args.url;
    this.status = args.status;
    this.body = args.body;
  }
}

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

// API base. Reads from NEXT_PUBLIC_API_BASE_V2 at build time (Next inlines
// NEXT_PUBLIC_* env vars into the client bundle). The default points at a
// local mock running on :3001 for development — the real deployment swaps
// it for the API Gateway URL.
export function apiV2Base(): string {
  const fromEnv = process.env.NEXT_PUBLIC_API_BASE_V2;
  if (fromEnv && fromEnv.length > 0) return fromEnv.replace(/\/$/, "");
  return "http://localhost:3001";
}

// ---------------------------------------------------------------------------
// Internal fetch wrapper
// ---------------------------------------------------------------------------

interface RequestOpts {
  signal?: AbortSignal;
}

async function postJson<TBody, TResp>(
  path: string,
  body: TBody,
  opts: RequestOpts = {},
): Promise<TResp> {
  const url = `${apiV2Base()}${path}`;
  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
      signal: opts.signal,
      cache: "no-store",
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiV2Error({
      kind: "offline",
      url,
      message: err instanceof Error ? err.message : "Network request failed",
    });
  }
  return parseJsonOrThrow<TResp>(url, response);
}

async function getJson<TResp>(
  path: string,
  opts: RequestOpts = {},
): Promise<TResp> {
  const url = `${apiV2Base()}${path}`;
  let response: Response;
  try {
    response = await fetch(url, {
      method: "GET",
      headers: { Accept: "application/json" },
      signal: opts.signal,
      cache: "no-store",
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiV2Error({
      kind: "offline",
      url,
      message: err instanceof Error ? err.message : "Network request failed",
    });
  }
  return parseJsonOrThrow<TResp>(url, response);
}

async function parseJsonOrThrow<T>(url: string, response: Response): Promise<T> {
  if (!response.ok) {
    let body: unknown;
    try {
      const text = await response.clone().text();
      if (text) body = JSON.parse(text);
    } catch {
      body = undefined;
    }
    throw new ApiV2Error({
      kind: "http",
      url,
      status: response.status,
      message: `HTTP ${response.status} ${response.statusText}`,
      body,
    });
  }
  try {
    return (await response.json()) as T;
  } catch (err) {
    throw new ApiV2Error({
      kind: "parse",
      url,
      message: err instanceof Error ? err.message : "Response was not valid JSON",
    });
  }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export async function createUrlJob(
  url: string,
  opts: RequestOpts = {},
): Promise<CreateUrlJobResponse> {
  return postJson<{ url: string }, CreateUrlJobResponse>(
    "/v2/jobs",
    { url },
    opts,
  );
}

export async function createUploadJob(
  filename: string,
  contentType: string,
  opts: RequestOpts = {},
): Promise<CreateUploadJobResponse> {
  return postJson<
    { filename: string; contentType: string },
    CreateUploadJobResponse
  >("/v2/jobs/upload", { filename, contentType }, opts);
}

export async function confirmUpload(
  jobId: string,
  opts: RequestOpts = {},
): Promise<void> {
  const url = `${apiV2Base()}/v2/jobs/upload/${encodeURIComponent(jobId)}/confirm`;
  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: { Accept: "application/json" },
      signal: opts.signal,
      cache: "no-store",
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiV2Error({
      kind: "offline",
      url,
      message: err instanceof Error ? err.message : "Network request failed",
    });
  }
  if (!response.ok) {
    throw new ApiV2Error({
      kind: "http",
      url,
      status: response.status,
      message: `HTTP ${response.status} ${response.statusText}`,
    });
  }
}

// Upload an MP4 file to the presigned URL returned by createUploadJob.
// S3 presigned PUTs are signed against the bytes + the Content-Type and
// reject anything that doesn't match — we forward the same content-type
// we declared at creation time.
export async function putUpload(
  uploadUrl: string,
  file: File,
  opts: RequestOpts = {},
): Promise<void> {
  let response: Response;
  try {
    response = await fetch(uploadUrl, {
      method: "PUT",
      body: file,
      headers: { "Content-Type": file.type || "video/mp4" },
      signal: opts.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiV2Error({
      kind: "offline",
      url: uploadUrl,
      message: err instanceof Error ? err.message : "Upload failed",
    });
  }
  if (!response.ok) {
    throw new ApiV2Error({
      kind: "http",
      url: uploadUrl,
      status: response.status,
      message: `HTTP ${response.status} ${response.statusText}`,
    });
  }
}

export async function getJob(
  jobId: string,
  opts: RequestOpts = {},
): Promise<JobStatusResponse> {
  return getJson<JobStatusResponse>(
    `/v2/jobs/${encodeURIComponent(jobId)}`,
    opts,
  );
}

// Fetch + validate the activation JSON at `resultUrl`. We don't trust the
// shape blindly — the upload path can be racy and a partially-written
// file would otherwise propagate as a runtime BrainMesh crash. Reject
// here so the error state can render instead.
export async function fetchActivation(
  resultUrl: string,
  opts: RequestOpts = {},
): Promise<ActivationPayload> {
  let response: Response;
  try {
    response = await fetch(resultUrl, {
      method: "GET",
      headers: { Accept: "application/json" },
      signal: opts.signal,
      cache: "no-store",
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiV2Error({
      kind: "offline",
      url: resultUrl,
      message: err instanceof Error ? err.message : "Network request failed",
    });
  }
  if (!response.ok) {
    throw new ApiV2Error({
      kind: "http",
      url: resultUrl,
      status: response.status,
      message: `HTTP ${response.status} ${response.statusText}`,
    });
  }
  let json: unknown;
  try {
    json = await response.json();
  } catch (err) {
    throw new ApiV2Error({
      kind: "parse",
      url: resultUrl,
      message: err instanceof Error ? err.message : "Activation was not valid JSON",
    });
  }
  return validateActivation(resultUrl, json);
}

function validateActivation(url: string, raw: unknown): ActivationPayload {
  if (typeof raw !== "object" || raw === null) {
    throw new ApiV2Error({
      kind: "validation",
      url,
      message: "Activation payload was not an object",
    });
  }
  const obj = raw as Record<string, unknown>;
  const videoDurationSec = obj.videoDurationSec;
  const timestamps = obj.timestamps;
  const byRegion = obj.byRegion;
  const modelVersion = obj.modelVersion;
  if (typeof videoDurationSec !== "number" || !Number.isFinite(videoDurationSec)) {
    throw new ApiV2Error({
      kind: "validation",
      url,
      message: "videoDurationSec missing or not a number",
    });
  }
  if (!Array.isArray(timestamps) || !timestamps.every((t) => typeof t === "number")) {
    throw new ApiV2Error({
      kind: "validation",
      url,
      message: "timestamps missing or not an array of numbers",
    });
  }
  if (typeof modelVersion !== "string") {
    throw new ApiV2Error({
      kind: "validation",
      url,
      message: "modelVersion missing or not a string",
    });
  }
  if (typeof byRegion !== "object" || byRegion === null) {
    throw new ApiV2Error({
      kind: "validation",
      url,
      message: "byRegion missing or not an object",
    });
  }
  const byRegionTyped: Record<string, number[]> = {};
  const byRegionRaw = byRegion as Record<string, unknown>;
  for (const region of REGION_IDS) {
    const series = byRegionRaw[region];
    if (!Array.isArray(series) || !series.every((v) => typeof v === "number")) {
      throw new ApiV2Error({
        kind: "validation",
        url,
        message: `byRegion.${region} missing or not an array of numbers`,
      });
    }
    if (series.length !== timestamps.length) {
      throw new ApiV2Error({
        kind: "validation",
        url,
        message: `byRegion.${region} length ${series.length} does not match timestamps length ${timestamps.length}`,
      });
    }
    byRegionTyped[region] = series as number[];
  }
  return {
    videoDurationSec,
    timestamps: timestamps as number[],
    byRegion: byRegionTyped as Record<RegionId, number[]>,
    modelVersion,
  };
}

// ---------------------------------------------------------------------------
// URL validation
// ---------------------------------------------------------------------------

// Loose TikTok URL recogniser. We accept the canonical tiktok.com/@user/video/<id>,
// the vm.tiktok.com / vt.tiktok.com short links, and any tiktok.com host —
// the backend will do the strict parse + validate; this is purely client-side
// gating for the Predict button.
export function looksLikeTikTokUrl(input: string): boolean {
  const trimmed = input.trim();
  if (trimmed.length === 0) return false;
  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    return false;
  }
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") return false;
  const host = parsed.hostname.toLowerCase();
  return (
    host === "tiktok.com" ||
    host.endsWith(".tiktok.com")
  );
}
