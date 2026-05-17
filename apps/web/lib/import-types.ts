// Proposed contract additions for the TikTok-export import flow. These
// shapes are NOT yet in `shared/types.ts` — terminal 1 will land the
// canonical copy once api-orchestrator's PR aligns. Mirror them here so
// the /import route can be built against a typed surface; replace with
// re-exports from `@shared/types` once they land.

import { API_BASE } from "@shared/types";

export type ImportJobStatus = "queued" | "running" | "complete" | "failed";

// Phase string is informational only — rendered after the dash on the
// /import status line. Suggested vocabulary: "parsing export",
// "downloading", "inferring", "aggregating". The frontend does not
// branch on the exact value.
export interface ImportJobProgress {
  current: number;
  total: number | null;
  phase: string | null;
}

export interface ImportJob {
  id: string;
  status: ImportJobStatus;
  created_at: string; // ISO-8601 UTC
  updated_at: string; // ISO-8601 UTC
  progress: ImportJobProgress;
  // Free-form error text. Populated when `status === "failed"`.
  error: string | null;
  // Filename the user dropped. Surfaced when a 409 returns the running
  // job so the UI can show "X is already importing".
  source_filename: string | null;
}

// Local endpoint constants — folded into `shared/types.ts:ENDPOINTS`
// when the contract lands.
export const IMPORT_ENDPOINTS = {
  importStart: `${API_BASE}/import`, // POST multipart, field name "file"
  importJob: (id: string) => `${API_BASE}/import/${id}`, // GET
} as const;
