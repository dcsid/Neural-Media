"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useReducedMotion } from "./hooks/useReducedMotion";

// Scrubber for the Video Detail view. Owned by brain-viz because it is the
// thing that drives the mesh's playhead — the <video> element itself lives
// in frontend-dashboard territory and only feeds `playheadSec` in via prop.
//
// Wiring on the consumer side (Detail page):
//
//   const video = useRef<HTMLVideoElement>(null);
//   const [t, setT] = useState(0);
//
//   <video ref={video} src={...}
//          onTimeUpdate={e => setT(e.currentTarget.currentTime)} />
//   <BrainMesh activation={...} keyframeVertices={kv}
//              timestamps={ts} playheadSec={t} />
//   <TimelineScrubber timestamps={ts} duration={duration}
//                     playheadSec={t}
//                     onSeek={s => { video.current!.currentTime = s; }} />
//
// The video element stays the source of truth for time. The scrubber writes
// into it via onSeek; useActivationFrame then re-derives the brain frame.
//
// Framer Motion is not pulled in here — the only animation is a CSS
// transform on the thumb, which is cheaper. GSAP would be overkill and
// would not justify the dep weight per the brief.

export interface TimelineScrubberProps {
  // Timepoints from ActivationOutput.timestamps. Used to draw keyframe ticks
  // so the user can see where the model has real data vs interpolation.
  timestamps: number[];
  // Total video duration in seconds. Drives the scrubber's coordinate space.
  duration: number;
  // Current playhead position. Updated by the parent on the <video>
  // element's `timeupdate` event.
  playheadSec: number;
  // Called when the user drags or clicks the track. The parent should write
  // this value back to the <video> element's currentTime.
  onSeek?: (sec: number) => void;
  onScrubStart?: () => void;
  onScrubEnd?: () => void;
  className?: string;
}

function pct(n: number, d: number): string {
  if (d <= 0) return "0%";
  return `${Math.max(0, Math.min(100, (n / d) * 100))}%`;
}

export function TimelineScrubber({
  timestamps,
  duration,
  playheadSec,
  onSeek,
  onScrubStart,
  onScrubEnd,
  className,
}: TimelineScrubberProps) {
  const trackRef = useRef<HTMLDivElement>(null);
  const draggingRef = useRef(false);
  const [hovering, setHovering] = useState(false);
  const reduceMotion = useReducedMotion();

  const ticks = useMemo(() => {
    if (!duration || timestamps.length === 0) return [];
    // Cap the number of rendered tick marks; if `timestamps` is densely
    // sampled (e.g. one per fMRI TR) we'd blow out the DOM otherwise.
    const MAX = 64;
    if (timestamps.length <= MAX) return timestamps;
    const stride = Math.ceil(timestamps.length / MAX);
    const out: number[] = [];
    for (let i = 0; i < timestamps.length; i += stride) out.push(timestamps[i]);
    return out;
  }, [timestamps, duration]);

  const seekFromClientX = useCallback(
    (clientX: number) => {
      const el = trackRef.current;
      if (!el || !duration || !onSeek) return;
      const rect = el.getBoundingClientRect();
      const f = (clientX - rect.left) / rect.width;
      const t = Math.max(0, Math.min(duration, f * duration));
      onSeek(t);
    },
    [duration, onSeek],
  );

  const onPointerDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (!onSeek) return;
      draggingRef.current = true;
      onScrubStart?.();
      (e.target as HTMLElement).setPointerCapture(e.pointerId);
      seekFromClientX(e.clientX);
    },
    [seekFromClientX, onSeek, onScrubStart],
  );

  const onPointerMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (!draggingRef.current) return;
      seekFromClientX(e.clientX);
    },
    [seekFromClientX],
  );

  const onPointerUp = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (!draggingRef.current) return;
      draggingRef.current = false;
      onScrubEnd?.();
      try {
        (e.target as HTMLElement).releasePointerCapture(e.pointerId);
      } catch {
        // setPointerCapture may not have succeeded on touch; harmless.
      }
    },
    [onScrubEnd],
  );

  // Keyboard scrub: ← / → step 1s, shift accelerates 5s. The track is
  // focusable when onSeek is wired up.
  const onKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      if (!onSeek || !duration) return;
      let step = 0;
      if (e.key === "ArrowLeft") step = -1;
      else if (e.key === "ArrowRight") step = 1;
      else if (e.key === "Home") step = -playheadSec;
      else if (e.key === "End") step = duration - playheadSec;
      else return;
      e.preventDefault();
      if (e.shiftKey && (e.key === "ArrowLeft" || e.key === "ArrowRight")) {
        step *= 5;
      }
      onSeek(Math.max(0, Math.min(duration, playheadSec + step)));
    },
    [duration, onSeek, playheadSec],
  );

  // The transform-only thumb position keeps the track on the GPU compositor
  // and avoids layout work during scrub.
  const thumbStyle = useMemo(() => {
    const left = pct(playheadSec, duration);
    return {
      left,
      transition: reduceMotion || draggingRef.current ? "none" : "left 80ms linear",
    } as React.CSSProperties;
  }, [playheadSec, duration, reduceMotion]);

  // Make sure dragging state resets if the parent unmounts while scrubbing.
  useEffect(() => () => {
    draggingRef.current = false;
  }, []);

  return (
    <div
      className={["select-none", className ?? ""].join(" ")}
      onMouseEnter={() => setHovering(true)}
      onMouseLeave={() => setHovering(false)}
    >
      <div
        ref={trackRef}
        role="slider"
        aria-label="Video timeline"
        aria-valuemin={0}
        aria-valuemax={duration || 0}
        aria-valuenow={Math.round(playheadSec * 100) / 100}
        tabIndex={onSeek ? 0 : -1}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onKeyDown={onKeyDown}
        className={[
          "relative h-6 cursor-pointer outline-none",
          "before:absolute before:left-0 before:right-0 before:top-1/2 before:h-px",
          "before:-translate-y-1/2 before:bg-line",
        ].join(" ")}
      >
        {/* Keyframe ticks. Light when idle, brighter on hover. */}
        {ticks.map((t, i) => (
          <span
            key={i}
            aria-hidden
            className={[
              "pointer-events-none absolute top-1/2 h-1.5 w-px -translate-y-1/2",
              hovering ? "bg-ink-300" : "bg-ink-500",
            ].join(" ")}
            style={{ left: pct(t, duration) }}
          />
        ))}

        {/* Played-fill. */}
        <span
          aria-hidden
          className="pointer-events-none absolute left-0 top-1/2 h-px -translate-y-1/2 bg-accent"
          style={{ width: pct(playheadSec, duration) }}
        />

        {/* Thumb. */}
        <span
          aria-hidden
          className={[
            "pointer-events-none absolute top-1/2 h-3 w-px -translate-x-1/2 -translate-y-1/2",
            "bg-accent",
          ].join(" ")}
          style={thumbStyle}
        />
      </div>
    </div>
  );
}

export default TimelineScrubber;
