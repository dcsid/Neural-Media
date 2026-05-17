"use client";

// Integration glue between brain-viz components and the Video Detail page.
//
// Composes:
//   - BrainMesh (brain-viz)        — renders the cortical surface, driven by
//                                     `playheadSec` against `keyframeVertices`
//                                     + `timestamps`.
//   - TimelineScrubber (brain-viz) — writes back `playheadSec` on user input.
//
// No <video> element here — TikTok URLs aren't direct video sources, so the
// scrubber IS the source of truth for time during MVP. When the data-pipeline
// worker lands local downloads, swap a `<video>` element in front of this
// component and let it own `currentTime` per the pattern documented inside
// TimelineScrubber.tsx.
//
// BrainMesh is loaded via next/dynamic with ssr:false so R3F's reconciler
// never runs in the server-render path — same pattern as BrainMeshSlot.

import dynamic from "next/dynamic";
import { useState } from "react";
// Import TimelineScrubber from the file directly rather than the barrel
// so SSR doesn't transitively evaluate BrainMesh / R3F.
import { TimelineScrubber } from "@/components/brain/TimelineScrubber";
import type { ActivationOutput } from "@shared/types";

const BrainMeshLazy = dynamic(
  () =>
    import("@/components/brain/BrainMesh").then((mod) => ({
      default: mod.BrainMesh,
    })),
  {
    ssr: false,
    loading: () => (
      <div
        aria-hidden
        className="absolute inset-0 bg-[radial-gradient(circle_at_center,rgba(245,165,36,0.05),transparent_60%)]"
      />
    ),
  },
);

interface BrainDetailViewerProps {
  activation: ActivationOutput;
  meanActivation: number;
  durationS: number;
}

export function BrainDetailViewer({
  activation,
  meanActivation,
  durationS,
}: BrainDetailViewerProps) {
  const [playheadSec, setPlayheadSec] = useState(0);

  return (
    <div>
      <div className="relative aspect-[5/4] w-full overflow-hidden border border-line bg-canvas">
        <BrainMeshLazy
          activation={meanActivation}
          keyframeVertices={activation.keyframe_vertices}
          timestamps={activation.timestamps}
          playheadSec={playheadSec}
        />
      </div>
      <div className="mt-4">
        <TimelineScrubber
          timestamps={activation.timestamps}
          duration={durationS}
          playheadSec={playheadSec}
          onSeek={setPlayheadSec}
        />
        <div className="mt-2 flex justify-between font-mono text-[10px] tabular-nums text-ink-400">
          <span>{playheadSec.toFixed(2)}s</span>
          <span>{durationS.toFixed(2)}s</span>
        </div>
      </div>
    </div>
  );
}
