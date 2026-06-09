"use client";

import {
  createContext,
  useContext,
  useState,
  type Dispatch,
  type ReactNode,
  type SetStateAction,
} from "react";
import type {
  ActivationPayload,
  JobStatus,
  TerminalFailureStatus,
} from "@/lib/api-v2";

// ---------------------------------------------------------------------------
// Home-page state machine — lifted out of app/page.tsx into a layout-level
// store so the current upload + result survive client navigation
// (/ ↔ /gallery) without re-uploading or re-running inference. The product is
// upload-only: drop an MP4, pick a ≤90s window, watch the synced video+brain.
// ---------------------------------------------------------------------------

export interface IdleState {
  kind: "idle";
  // The uploaded MP4 (null until the user picks one → dropzone shown).
  file: File | null;
  // Duration read from the file's own metadata (null while probing / no file).
  fileDurationSec: number | null;
  // Inline message if the picked file's metadata couldn't be read.
  fileError?: string;
  // Segment window as raw input strings, parsed + validated at submit.
  startInput: string;
  endInput: string;
  // Submission-time error (create/put/confirm 4xx, network). Shown inline.
  submitError?: string;
  // Set while the create→put→confirm chain is in flight.
  submitting?: boolean;
}

export interface TrackingState {
  kind: "tracking";
  jobId: string;
  status: JobStatus;
  elapsedSec: number;
  // Wall-clock ms at which polling started (enforces the overall cap).
  startedAtMs: number;
  // Carried from the upload so the result can play the LOCAL file, trimmed to
  // the analyzed [startSec, endSec) window.
  file: File;
  startSec: number;
  endSec: number;
}

// "network" = polling died with no actionable status; "timeout" = overall cap.
export type ErrorKind = TerminalFailureStatus | "network" | "timeout";

export interface ErrorState {
  kind: "error";
  errorKind: ErrorKind;
  // Server error_code — drives copy, never shown raw.
  errorCode?: string;
}

export interface ResultState {
  kind: "result";
  jobId: string;
  activation: ActivationPayload;
  // The local upload + where its analyzed window starts, for the synced player.
  file: File;
  startSec: number;
}

export type Phase = IdleState | TrackingState | ResultState | ErrorState;

export function freshIdle(): IdleState {
  return {
    kind: "idle",
    file: null,
    fileDurationSec: null,
    startInput: "0",
    endInput: "0",
  };
}

// ---------------------------------------------------------------------------
// Session store (in-memory, layout-scoped)
//
// The provider lives in the root layout, which does NOT unmount on client
// navigation — so `phase` (including the uploaded File and the fetched
// activation, both already in browser memory) rides across / ↔ /gallery. A full
// reload remounts the layout and resets to a fresh idle, and a new upload
// replaces it: exactly the "keep until reload or new upload" lifetime. Nothing
// is persisted to disk; returning to / skips the re-upload AND the re-inference.
// ---------------------------------------------------------------------------

interface SessionPhase {
  phase: Phase;
  setPhase: Dispatch<SetStateAction<Phase>>;
}

const SessionPhaseContext = createContext<SessionPhase | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [phase, setPhase] = useState<Phase>(freshIdle);
  return (
    <SessionPhaseContext.Provider value={{ phase, setPhase }}>
      {children}
    </SessionPhaseContext.Provider>
  );
}

export function useSessionPhase(): SessionPhase {
  const ctx = useContext(SessionPhaseContext);
  if (!ctx) {
    throw new Error("useSessionPhase must be used within <SessionProvider>");
  }
  return ctx;
}
