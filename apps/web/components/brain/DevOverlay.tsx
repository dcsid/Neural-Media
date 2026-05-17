"use client";

import { useFrame, useThree } from "@react-three/fiber";
import { useEffect, useRef, useState } from "react";

// Performance HUD, opt-in via ?dev=1 on the URL. Reads renderer info each
// frame and reports it to an HTML overlay sibling.
//
// Tracked: instantaneous FPS, draw calls per frame, triangle count, and
// JS-heap usage if the browser exposes `performance.memory` (Chromium only).
// The brief budget is 60fps / <250MB / first paint <2s; this is the
// instrument we use to verify it.

export interface DevSample {
  fps: number;
  draws: number;
  triangles: number;
  heapMb: number | null;
  firstPaintMs: number | null;
}

interface DevProbeProps {
  onSample: (s: DevSample) => void;
  firstPaintMs: number | null;
}

// Lives inside <Canvas>. Cannot render HTML — only feeds samples out.
export function DevProbe({ onSample, firstPaintMs }: DevProbeProps) {
  const gl = useThree((s) => s.gl);
  const acc = useRef({ frames: 0, last: performance.now() });

  useFrame(() => {
    acc.current.frames += 1;
    const now = performance.now();
    const elapsed = now - acc.current.last;
    if (elapsed >= 500) {
      const fps = (acc.current.frames * 1000) / elapsed;
      const info = gl.info.render;
      const perfMem = (
        performance as unknown as {
          memory?: { usedJSHeapSize: number };
        }
      ).memory;
      const heapMb = perfMem ? perfMem.usedJSHeapSize / (1024 * 1024) : null;
      onSample({
        fps,
        draws: info.calls,
        triangles: info.triangles,
        heapMb,
        firstPaintMs,
      });
      acc.current.frames = 0;
      acc.current.last = now;
    }
  });

  return null;
}

interface DevOverlayProps {
  sample: DevSample | null;
}

// HTML side of the HUD. Pinned top-left of the BrainMesh container.
export function DevOverlay({ sample }: DevOverlayProps) {
  if (!sample) return null;
  return (
    <div
      className={[
        "pointer-events-none absolute left-2 top-2 z-10",
        "border border-line bg-surface/85 px-2 py-1.5",
        "font-mono text-[10px] leading-tight tabular-nums text-ink-100",
      ].join(" ")}
    >
      <div>
        <span className="text-ink-400">fps</span>{" "}
        <span className={sample.fps < 45 ? "text-accent" : "text-ink-50"}>
          {sample.fps.toFixed(1)}
        </span>
      </div>
      <div>
        <span className="text-ink-400">draws</span> {sample.draws}
      </div>
      <div>
        <span className="text-ink-400">tris</span>{" "}
        {sample.triangles.toLocaleString()}
      </div>
      {sample.heapMb !== null && (
        <div>
          <span className="text-ink-400">heap</span>{" "}
          <span
            className={
              sample.heapMb > 250 ? "text-accent" : "text-ink-50"
            }
          >
            {sample.heapMb.toFixed(0)} MB
          </span>
        </div>
      )}
      {sample.firstPaintMs !== null && (
        <div>
          <span className="text-ink-400">first paint</span>{" "}
          <span
            className={
              sample.firstPaintMs > 2000 ? "text-accent" : "text-ink-50"
            }
          >
            {sample.firstPaintMs.toFixed(0)} ms
          </span>
        </div>
      )}
    </div>
  );
}

// Gate the overlay on `?dev=1`. Reads the URL once on mount; we don't need
// reactivity here.
export function useDevModeEnabled(): boolean {
  const [on, setOn] = useState(false);
  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    setOn(params.get("dev") === "1");
  }, []);
  return on;
}
