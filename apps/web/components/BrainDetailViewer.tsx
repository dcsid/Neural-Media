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

import { useState } from "react";
import { BrainMesh, TimelineScrubber } from "@/components/brain";
import type { ActivationOutput } from "@shared/types";

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
      <div className="relative aspect-[5/4] w-full border border-line bg-canvas">
        <BrainMesh
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
