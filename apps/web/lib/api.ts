import {
  ENDPOINTS,
  type ActivationOutput,
  type AggregateReport,
  type InferenceRun,
  type RegionDef,
  type RegionMetrics,
  type VideoMetadata,
  type WatchEvent,
} from "@shared/types";

// Local fetch errors are normalised to this single class so callers can
// distinguish "server unreachable" from "server replied with HTTP 4xx/5xx"
// when rendering the API-offline empty state.
export class ApiError extends Error {
  readonly kind: "offline" | "http" | "parse";
  readonly status?: number;
  readonly url: string;

  constructor(args: {
    kind: "offline" | "http" | "parse";
    url: string;
    message: string;
    status?: number;
  }) {
    super(args.message);
    this.name = "ApiError";
    this.kind = args.kind;
    this.status = args.status;
    this.url = args.url;
  }
}

export interface ApiFetchOptions {
  signal?: AbortSignal;
  // Server Components run on the Node side and need an absolute URL.
  // We default to the rewrite-friendly relative path, but pages running
  // server-side pass the request origin so fetch works either way.
  baseUrl?: string;
  // Forwarded to Next's fetch caching layer. Default is no-store so the
  // dashboard always reflects the latest local API state.
  cache?: RequestCache;
  next?: { revalidate?: number | false; tags?: string[] };
}

async function apiFetch<T>(path: string, opts: ApiFetchOptions = {}): Promise<T> {
  const url = opts.baseUrl ? `${opts.baseUrl.replace(/\/$/, "")}${path}` : path;
  let response: Response;
  try {
    response = await fetch(url, {
      signal: opts.signal,
      cache: opts.cache ?? "no-store",
      next: opts.next,
      headers: { Accept: "application/json" },
    });
  } catch (err) {
    throw new ApiError({
      kind: "offline",
      url,
      message: err instanceof Error ? err.message : "Network request failed",
    });
  }

  if (!response.ok) {
    throw new ApiError({
      kind: "http",
      url,
      status: response.status,
      message: `HTTP ${response.status} ${response.statusText}`,
    });
  }

  try {
    return (await response.json()) as T;
  } catch (err) {
    throw new ApiError({
      kind: "parse",
      url,
      message: err instanceof Error ? err.message : "Response was not valid JSON",
    });
  }
}

export const api = {
  health: (opts?: ApiFetchOptions) =>
    apiFetch<{ status: string }>(ENDPOINTS.health, opts),
  videos: (opts?: ApiFetchOptions) =>
    apiFetch<VideoMetadata[]>(ENDPOINTS.videos, opts),
  video: (id: string, opts?: ApiFetchOptions) =>
    apiFetch<VideoMetadata>(ENDPOINTS.video(id), opts),
  videoMetrics: (id: string, opts?: ApiFetchOptions) =>
    apiFetch<RegionMetrics[]>(ENDPOINTS.videoMetrics(id), opts),
  videoActivation: (id: string, opts?: ApiFetchOptions) =>
    apiFetch<ActivationOutput>(ENDPOINTS.videoActivation(id), opts),
  regions: (opts?: ApiFetchOptions) =>
    apiFetch<RegionDef[]>(ENDPOINTS.regions, opts),
  aggregate: (opts?: ApiFetchOptions) =>
    apiFetch<AggregateReport>(ENDPOINTS.aggregate, opts),
  watchEvents: (opts?: ApiFetchOptions) =>
    apiFetch<WatchEvent[]>(ENDPOINTS.watchEvents, opts),
  inferenceRuns: (opts?: ApiFetchOptions) =>
    apiFetch<InferenceRun[]>(ENDPOINTS.inferenceRuns, opts),
};

// Server components need an absolute URL to hit the Next.js rewrite layer.
// In Node we know we're talking to the same dev server.
export function serverBaseUrl(): string {
  const port = process.env.PORT ?? "3000";
  return process.env.NEURAL_MEDIA_WEB_BASE ?? `http://127.0.0.1:${port}`;
}
