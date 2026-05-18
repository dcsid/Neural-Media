import {
  ENDPOINTS,
  type ActivationOutput,
  type AggregateReport,
  type Capabilities,
  type ImportJob,
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
  // Raw parsed JSON body when the server returned one alongside the
  // error — surfaces e.g. the running ImportJob on a 409.
  readonly body?: unknown;

  constructor(args: {
    kind: "offline" | "http" | "parse";
    url: string;
    message: string;
    status?: number;
    body?: unknown;
  }) {
    super(args.message);
    this.name = "ApiError";
    this.kind = args.kind;
    this.status = args.status;
    this.url = args.url;
    this.body = args.body;
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
  // When true, append `?demo=1` to the URL so the API serves from the
  // demo store rather than the live catalogue. Silently a no-op on the
  // backend if no demo dataset is on disk (falls back to live store).
  demo?: boolean;
}

async function apiFetch<T>(path: string, opts: ApiFetchOptions = {}): Promise<T> {
  const withDemo = opts.demo
    ? `${path}${path.includes("?") ? "&" : "?"}demo=1`
    : path;
  const url = opts.baseUrl ? `${opts.baseUrl.replace(/\/$/, "")}${withDemo}` : withDemo;
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
    // Best-effort: capture the error body if it's JSON so callers can
    // inspect e.g. the running job returned by a 409 on /import.
    let body: unknown;
    try {
      const text = await response.clone().text();
      if (text) body = JSON.parse(text);
    } catch {
      body = undefined;
    }
    throw new ApiError({
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
  importStart: (
    file: File,
    extra?: {
      days?: number;
      since?: string;
      until?: string;
      mode?: "mock" | "real";
    },
    opts?: ApiFetchOptions,
  ) =>
    apiFetchMultipart<ImportJob>(ENDPOINTS.importStart, file, extra, opts),
  importJob: (id: string, opts?: ApiFetchOptions) =>
    apiFetch<ImportJob>(ENDPOINTS.importJob(id), opts),
  capabilities: (opts?: ApiFetchOptions) =>
    apiFetch<Capabilities>(ENDPOINTS.capabilities, opts),
};

async function apiFetchMultipart<T>(
  path: string,
  file: File,
  extra: {
    days?: number;
    since?: string;
    until?: string;
    mode?: "mock" | "real";
  } = {},
  opts: ApiFetchOptions = {},
): Promise<T> {
  const url = opts.baseUrl ? `${opts.baseUrl.replace(/\/$/, "")}${path}` : path;
  const form = new FormData();
  form.append("file", file, file.name);
  if (extra.days != null) form.append("days", String(extra.days));
  if (extra.since) form.append("since", extra.since);
  if (extra.until) form.append("until", extra.until);
  if (extra.mode) form.append("mode", extra.mode);

  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      body: form,
      signal: opts.signal,
      cache: "no-store",
      headers: { Accept: "application/json" },
      // Do NOT set Content-Type — the browser fills in the multipart
      // boundary when the body is a FormData.
    });
  } catch (err) {
    throw new ApiError({
      kind: "offline",
      url,
      message: err instanceof Error ? err.message : "Network request failed",
    });
  }

  if (!response.ok) {
    let body: unknown;
    try {
      const text = await response.clone().text();
      if (text) body = JSON.parse(text);
    } catch {
      body = undefined;
    }
    throw new ApiError({
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
    throw new ApiError({
      kind: "parse",
      url,
      message: err instanceof Error ? err.message : "Response was not valid JSON",
    });
  }
}

// Server components need an absolute URL to hit the Next.js rewrite layer.
// In Node we know we're talking to the same dev server.
export function serverBaseUrl(): string {
  const port = process.env.PORT ?? "3000";
  return process.env.NEURAL_MEDIA_WEB_BASE ?? `http://127.0.0.1:${port}`;
}
