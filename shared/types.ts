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
