// Neural Media — TypeScript contracts.
// Mirror of shared/schemas.py and shared/CONTRACTS.md.
// Any change to one MUST be reflected in the other two in the same PR.

export const REGION_IDS = [
  "v1",
  "v2",
  "v3",
  "v4",
  "auditory",
  "language",
  "ffa",
  "vwfa",
] as const;

export type RegionId = (typeof REGION_IDS)[number];

export const REGION_DESCRIPTIONS: Record<RegionId, string> = {
  v1: "Primary visual cortex",
  v2: "Secondary visual cortex",
  v3: "Tertiary visual cortex",
  v4: "V4 (color/form)",
  auditory: "Primary + belt auditory cortex",
  language: "Lateral language network",
  ffa: "Fusiform face area",
  vwfa: "Visual word form area",
};

export const NUM_VERTICES = 20_484 as const;

// ---------------------------------------------------------------------------
// Core records
// ---------------------------------------------------------------------------

export type ISODateString = string;

export interface VideoMetadata {
  id: string;
  source_url: string;
  title: string | null;
  author: string | null;
  duration_s: number;
  downloaded: boolean;
  local_path: string | null;
  tags: string[];
}

export interface WatchEvent {
  id: string;
  video_id: string;
  watched_at: ISODateString;
  duration_watched_s: number | null;
  completion_pct: number | null;
  source: "tiktok_export";
}

export type InferenceStatus = "pending" | "running" | "complete" | "failed";

export interface InferenceRun {
  id: string;
  video_id: string;
  model_id: string;
  model_version: string;
  seed: number;
  params_json: Record<string, unknown>;
  created_at: ISODateString;
  activation_path: string;
  status: InferenceStatus;
}

export interface RegionMetrics {
  region_id: RegionId;
  video_id: string;
  inference_run_id: string;
  mean: number;
  peak: number;
  sustained: number;
  timeseries: number[];
}

export interface RegionDef {
  region_id: RegionId;
  description: string;
}

export interface AggregateBucket {
  mean: number;
  peak: number;
}

export interface ClusterSummary {
  cluster_id: number;
  size: number;
  exemplar_video_ids: string[];
}

// Per-author rollup for the dashboard's "who you watch" panel.
// `top_region` is the region with the highest per-author peak across
// the author's videos. Capped at top 20 by `videos` desc. See
// CONTRACTS.md §6 and docs/worker-briefs/aggregate-by-author-proposal.md.
export interface AuthorBucket {
  author: string | null;
  videos: number;
  total_watch_time_s: number;
  mean_activation: number;
  top_region: RegionId;
}

export interface AggregateReport {
  total_videos: number;
  total_watch_time_s: number;
  first_watched_at: ISODateString | null;
  last_watched_at: ISODateString | null;
  by_region: Record<RegionId, AggregateBucket>;
  by_hour_of_day: number[]; // length 24
  by_day_of_week: number[]; // length 7  (Mon=0)
  by_author: AuthorBucket[]; // may be empty until aggregator ships
  clusters: ClusterSummary[];
}

// ---------------------------------------------------------------------------
// Activation envelope
// ---------------------------------------------------------------------------

export interface ActivationSidecar {
  inference_run_id: string;
  video_id: string;
  num_vertices: number;
  num_timepoints: number;
  sample_rate_hz: number;
  model_id: string;
  seed: number;
}

export interface ActivationOutput {
  inference_run_id: string;
  video_id: string;
  num_vertices: number;
  num_timepoints: number;
  sample_rate_hz: number;
  timestamps: number[];
  keyframe_vertices: Record<string, number[]>;
  region_means: Record<RegionId, number[]>;
}

// ---------------------------------------------------------------------------
// Import job (CONTRACTS.md §8)
// ---------------------------------------------------------------------------

export type ImportJobStatus =
  | "queued"
  | "running"
  | "complete"
  | "partial"
  | "failed";

export type ImportMode = "mock" | "real";

export type ImportPhase =
  | "parsing"
  | "downloading"
  | "preprocessing"
  | "inferring";

export interface ImportJobProgress {
  current: number;
  total: number | null;
  // Free-form phase string. The four documented values are listed in
  // `ImportPhase`, but the frontend does not branch on them — it only
  // renders the string after a dash on the status line.
  phase: string | null;
}

export interface ImportJob {
  id: string;
  status: ImportJobStatus;
  mode: ImportMode;
  created_at: ISODateString;
  updated_at: ISODateString;
  completed_at: ISODateString | null;
  progress: ImportJobProgress;
  error: string | null;
  source_filename: string | null;
}

// ---------------------------------------------------------------------------
// Capabilities (GET /api/v1/capabilities) — what the backend can run on the
// current host. `real_blockers` is a list of short tokens (e.g.
// "missing-extra", "missing-ffmpeg", "missing-yt-dlp", "missing-gpu") when
// `real` is false.
// ---------------------------------------------------------------------------

export interface Capabilities {
  mock: boolean;
  real: boolean;
  real_blockers: string[];
}

// ---------------------------------------------------------------------------
// API endpoint paths — used by the frontend, kept here so the contract is
// type-checked.
// ---------------------------------------------------------------------------

export const API_BASE = "/api/v1" as const;

export const ENDPOINTS = {
  health: `${API_BASE}/health`,
  videos: `${API_BASE}/videos`,
  video: (id: string) => `${API_BASE}/videos/${id}`,
  videoMetrics: (id: string) => `${API_BASE}/videos/${id}/metrics`,
  videoActivation: (id: string) => `${API_BASE}/videos/${id}/activation`,
  regions: `${API_BASE}/regions`,
  aggregate: `${API_BASE}/aggregate`,
  watchEvents: `${API_BASE}/watch-events`,
  inferenceRuns: `${API_BASE}/inference-runs`,
  importStart: `${API_BASE}/import`,
  importJob: (id: string) => `${API_BASE}/import/${id}`,
  capabilities: `${API_BASE}/capabilities`,
} as const;
