"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useReducedMotion } from "./hooks/useReducedMotion";
import {
  clampSeconds,
  isAtEnd,
  resolveSnapTolerance,
  secondsFromClientX,
  shouldAutoPauseAtEnd,
  snapToKeyframe,
} from "./scrubberMath";

// Scrubber for the Video Detail view. Owned by brain-viz because it is the
// thing that drives the mesh's playhead — the <video> element itself (when
// one exists) lives in frontend-dashboard territory and only feeds
// `playheadSec` in via prop. When there is no <video> the scrubber's own
// internal play loop is the source of time.
//
// Wiring on the consumer side (Detail page, video-backed):
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
//                     onSeek={s => { video.current!.currentTime = s; }}
//                     // Optional: snap drags / clicks to known keyframes
//                     keyframeTimes={Object.keys(activation.keyframe_vertices)
//                                       .map(Number).sort((a,b)=>a-b)} />
//
// Without a <video>, omit the ref/onTimeUpdate and let the scrubber's
// internal play loop advance `playheadSec` via `onSeek`.
//
// Framer Motion is not pulled in here — the only animation is a CSS
// transform on the thumb, which is cheaper. GSAP would be overkill and
// would not justify the dep weight per the brief.

export interface TimelineScrubberProps {
  // Dense timeseries timepoints from ActivationOutput.timestamps. Drawn as
  // faint ticks so the user can see the model's sampling cadence.
  timestamps: number[];
  // Sparse keyframe timestamps (Object.keys(keyframe_vertices) → number[]).
  // Drawn as taller, brighter ticks; clicks/drags within `snapToleranceSec`
  // of a keyframe snap to it.
  keyframeTimes?: number[];
  // Total video duration in seconds. Drives the scrubber's coordinate space.
  duration: number;
  // Current playhead position. Updated by the parent on the <video>
  // element's `timeupdate` event, or by the scrubber's own play loop when
  // no video element is present.
  playheadSec: number;
  // Called when the user drags or clicks the track. The parent should write
  // this value back to the <video> element's currentTime (or to its own
  // state when running video-less).
  onSeek?: (sec: number) => void;
  onScrubStart?: () => void;
  onScrubEnd?: () => void;
  // Tolerance for keyframe snap. Default 4% of duration. Pass 0 to disable.
  snapToleranceSec?: number;
  className?: string;
}

function pct(n: number, d: number): string {
  if (d <= 0) return "0%";
  return `${Math.max(0, Math.min(100, (n / d) * 100))}%`;
}

// 00:00.00 — minutes / seconds / centiseconds. mm grows past 99:59.99 for
// extremely long inputs but TikToks max out at ~10 minutes, so two digits
// is plenty visually.
function formatTime(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) sec = 0;
  const m = Math.floor(sec / 60);
  const s = sec - m * 60;
  const ss = s.toFixed(2).padStart(5, "0");
  return `${String(m).padStart(2, "0")}:${ss}`;
}

export function TimelineScrubber({
  timestamps,
  keyframeTimes,
  duration,
  playheadSec,
  onSeek,
  onScrubStart,
  onScrubEnd,
  snapToleranceSec,
  className,
}: TimelineScrubberProps) {
  const trackRef = useRef<HTMLDivElement>(null);
  const draggingRef = useRef(false);
  const [hovering, setHovering] = useState(false);
  const [playing, setPlaying] = useState(false);
  const reduceMotion = useReducedMotion();

  // Density-thinned ticks for the timeseries cadence. Cap at MAX so a
  // tightly-sampled (T, ...) array doesn't blow out the DOM.
  const ticks = useMemo(() => {
    if (!duration || timestamps.length === 0) return [];
    const MAX = 64;
    if (timestamps.length <= MAX) return timestamps;
    const stride = Math.ceil(timestamps.length / MAX);
    const out: number[] = [];
    for (let i = 0; i < timestamps.length; i += stride) out.push(timestamps[i]);
    return out;
  }, [timestamps, duration]);

  const sortedKeyframes = useMemo(() => {
    if (!keyframeTimes || keyframeTimes.length === 0) return null;
    const filtered = keyframeTimes
      .filter((t) => Number.isFinite(t) && t >= 0 && t <= duration)
      .sort((a, b) => a - b);
    return filtered.length > 0 ? filtered : null;
  }, [keyframeTimes, duration]);

  const snapTol = useMemo(
    () => resolveSnapTolerance(snapToleranceSec, duration),
    [snapToleranceSec, duration],
  );

  const seekFromClientX = useCallback(
    (clientX: number) => {
      const el = trackRef.current;
      if (!el || !duration || !onSeek) return;
      const rect = el.getBoundingClientRect();
      const raw = secondsFromClientX(clientX, rect.left, rect.width, duration);
      onSeek(snapToKeyframe(raw, sortedKeyframes, snapTol));
    },
    [duration, onSeek, sortedKeyframes, snapTol],
  );

  const onPointerDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (!onSeek) return;
      draggingRef.current = true;
      setPlaying(false); // dragging implicitly pauses
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

  // Keyboard scrub: ← / → step 1s, shift accelerates 5s. Space toggles
  // play/pause. The track is focusable when onSeek is wired up.
  const onKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      if (!onSeek || !duration) return;
      if (e.key === " " || e.code === "Space") {
        e.preventDefault();
        setPlaying((p) => !p);
        return;
      }
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
      onSeek(clampSeconds(playheadSec + step, duration));
    },
    [duration, onSeek, playheadSec],
  );

  // Auto-pause when we hit the end. Wrap-around would be confusing for a
  // predicted-activation scrub; the user can replay from the start manually.
  useEffect(() => {
    if (shouldAutoPauseAtEnd(playing, playheadSec, duration)) {
      setPlaying(false);
    }
  }, [playing, playheadSec, duration]);

  // Internal play loop. rAF-driven so it stays in sync with the GL
  // renderer. Honours prefers-reduced-motion by stepping at half speed and
  // only invalidating once per second — enough to feel alive without
  // animating aggressively.
  useEffect(() => {
    if (!playing || !onSeek || duration <= 0) return;
    let raf = 0;
    let last = performance.now();
    const speed = reduceMotion ? 0.5 : 1;
    const loop = (now: number) => {
      const dt = (now - last) / 1000;
      last = now;
      const next = Math.min(duration, playheadSec + dt * speed);
      onSeek(next);
      if (next < duration) raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
    // playheadSec intentionally omitted from deps — including it would
    // cancel/restart the loop on every onSeek call. We snapshot it on the
    // closure once per play press and let `onSeek` keep updating it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing, onSeek, duration, reduceMotion]);

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

  const canPlay = !!onSeek && duration > 0;
  const atEnd = isAtEnd(playheadSec, duration);

  return (
    <div
      className={["select-none", className ?? ""].join(" ")}
      onMouseEnter={() => setHovering(true)}
      onMouseLeave={() => setHovering(false)}
    >
      <div className="flex items-center gap-3">
        <button
          type="button"
          aria-label={playing ? "Pause" : "Play"}
          aria-pressed={playing}
          disabled={!canPlay}
          onClick={() => {
            if (atEnd && onSeek) onSeek(0);
            setPlaying((p) => !p);
          }}
          className={[
            "flex h-6 w-6 shrink-0 items-center justify-center border border-line",
            "bg-surface text-ink-200 transition-colors",
            "hover:border-accent hover:text-accent focus-visible:border-accent",
            "focus-visible:text-accent focus-visible:outline-none",
            "disabled:cursor-not-allowed disabled:opacity-40",
          ].join(" ")}
        >
          {playing ? (
            <svg width="9" height="10" viewBox="0 0 9 10" aria-hidden>
              <rect x="0.5" y="0.5" width="2.5" height="9" fill="currentColor" />
              <rect x="6" y="0.5" width="2.5" height="9" fill="currentColor" />
            </svg>
          ) : (
            <svg width="9" height="10" viewBox="0 0 9 10" aria-hidden>
              <path d="M1 0.5 L8.5 5 L1 9.5 Z" fill="currentColor" />
            </svg>
          )}
        </button>

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
            "relative h-6 flex-1 cursor-pointer outline-none",
            "before:absolute before:left-0 before:right-0 before:top-1/2 before:h-px",
            "before:-translate-y-1/2 before:bg-line",
          ].join(" ")}
        >
          {/* Density ticks — model sampling cadence. */}
          {ticks.map((t, i) => (
            <span
              key={`d${i}`}
              aria-hidden
              className={[
                "pointer-events-none absolute top-1/2 h-1.5 w-px -translate-y-1/2",
                hovering ? "bg-ink-300" : "bg-ink-500",
              ].join(" ")}
              style={{ left: pct(t, duration) }}
            />
          ))}

          {/* Keyframe ticks — taller, brighter; clicks snap to these. */}
          {sortedKeyframes?.map((t, i) => (
            <span
              key={`k${i}`}
              aria-hidden
              title={`keyframe at ${t.toFixed(2)}s`}
              className="pointer-events-none absolute top-1/2 h-3 w-px -translate-y-1/2 bg-ink-200"
              style={{ left: pct(t, duration) }}
            />
          ))}

          {/* Played-fill. */}
          <span
            aria-hidden
            className="pointer-events-none absolute left-0 top-1/2 h-px -translate-y-1/2 bg-accent"
            style={{ width: pct(playheadSec, duration) }}
          />

          {/* Thumb — small circle so it's discoverable as draggable. */}
          <span
            aria-hidden
            className={[
              "pointer-events-none absolute top-1/2 h-2.5 w-2.5",
              "-translate-x-1/2 -translate-y-1/2 rounded-full bg-accent",
              "ring-2 ring-canvas",
            ].join(" ")}
            style={thumbStyle}
          />
        </div>
      </div>

      <div className="mt-1.5 flex items-baseline justify-between font-mono text-[10px] tabular-nums text-ink-400">
        <span aria-label="Current time">{formatTime(playheadSec)}</span>
        {sortedKeyframes && sortedKeyframes.length > 0 && (
          <span className="text-ink-500">
            {sortedKeyframes.length} keyframe{sortedKeyframes.length === 1 ? "" : "s"}
          </span>
        )}
        <span aria-label="Total duration">{formatTime(duration)}</span>
      </div>
    </div>
  );
}

export default TimelineScrubber;
