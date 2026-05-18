"use client";

import { useEffect, useState } from "react";
import dynamic from "next/dynamic";
import type { ActivationOutput } from "@shared/types";

// Auto-cycling brain-mesh viewer for the dashboard hero. Loops the
// scrubber forward at a fixed rate so the page reads as alive on first
// glance — no need to find the scrubber and drag it. Loops by wrapping
// `playheadSec` back to 0 when it passes the activation's last
// timestamp. Pauses on hover so the viewer can study a frame; click to
// jump the playhead to a specific point.
//
// Lazy-loaded with ssr:false so the R3F runtime never enters the
// server-render path — same pattern as BrainMeshSlot and
// BrainDetailViewer.

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

interface AutoPlayingBrainProps {
  activation: ActivationOutput;
  meanActivation: number;
  // Wall-clock seconds for one full loop of the timeline. Decoupled
  // from the activation's own `durationS` so a 5s clip can still
  // animate visibly for 10s — the dashboard is decorative, not
  // diagnostic. Default ~8s feels like a heartbeat without being
  // distracting.
  loopWallSeconds?: number;
}

export function AutoPlayingBrain({
  activation,
  meanActivation,
  loopWallSeconds = 8,
}: AutoPlayingBrainProps) {
  const [playheadSec, setPlayheadSec] = useState(0);
  const [paused, setPaused] = useState(false);

  const timestamps = activation.timestamps;
  const lastT = timestamps.at(-1) ?? 0;

  useEffect(() => {
    if (paused || lastT <= 0) return;
    // Step ~30 ticks/second — visually smooth and inexpensive next to
    // the GLB sampling cost. Wall-clock loopSeconds maps to lastT of
    // animation time so the visible cycle is always loopWallSeconds.
    const fps = 30;
    const dtAnim = lastT / (loopWallSeconds * fps);
    const id = window.setInterval(() => {
      setPlayheadSec((t) => {
        const next = t + dtAnim;
        return next >= lastT ? 0 : next;
      });
    }, 1000 / fps);
    return () => window.clearInterval(id);
  }, [paused, lastT, loopWallSeconds]);

  return (
    <div
      className="relative aspect-[5/4] w-full overflow-hidden border border-line bg-canvas"
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      title="Hover to pause"
    >
      <BrainMeshLazy
        activation={meanActivation}
        keyframeVertices={activation.keyframe_vertices}
        timestamps={timestamps}
        playheadSec={playheadSec}
      />
      <div className="pointer-events-none absolute bottom-0 left-0 right-0 px-3 py-2 font-mono text-[10px] tabular-nums text-ink-300">
        <span>{playheadSec.toFixed(2)}s / {lastT.toFixed(2)}s</span>
        <span className="ml-3 opacity-60">{paused ? "paused (hover)" : "playing"}</span>
      </div>
    </div>
  );
}
