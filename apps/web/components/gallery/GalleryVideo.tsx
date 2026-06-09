"use client";

import clsx from "clsx";
import { useEffect, useRef, useState } from "react";

import { useReducedMotion } from "@/components/brain/hooks/useReducedMotion";

// Isolated, fully parent-CONTROLLED native <video> for the demo gallery.
// It owns no playback policy of its own: the parent drives play/pause via
// `playing`, scrubbing via `seekRequest`, and reads time back through
// `onTime`. Wrapping a native <video> (NOT a YouTube embed) keeps the
// gallery $0, offline-capable, and frame-syncable with the brain mesh.
//
// Consumer (the gallery page, T4) wiring sketch:
//
//   const [playing, setPlaying] = useState(false);
//   const [seek, setSeek] = useState({ sec: 0, nonce: 0 });
//   <GalleryVideo src={clip.src} poster={clip.poster}
//                 playing={playing}
//                 seekRequest={seek}
//                 onLoaded={(d) => setDuration(d)}
//                 onTime={(t) => setPlayhead(t)}   // → drives <BrainMesh playheadSec>
//                 onEnded={() => setPlaying(false)} />
//
// NOTE (flagged to brain): the brief's typed prop block omitted `onTime`,
// but its BEHAVIOR section requires reporting playback time, and the brain
// mesh needs a time source. `onTime` is added here as an OPTIONAL prop, so
// it's additive — T4 code that ignores it still type-checks.

export interface GalleryVideoProps {
  /** Video source URL (e.g. a local `/demo-clips/<slug>.mp4`). */
  src: string;
  /** Optional poster shown before metadata loads and while paused at 0. */
  poster?: string;
  /** Controlled play/pause. The component never plays on its own. */
  playing: boolean;
  /**
   * Seek when `nonce` changes (so re-firing the *same* `sec` still seeks).
   * A seek requested before metadata is ready is applied once it loads.
   */
  seekRequest?: { sec: number; nonce: number };
  /** Fired once metadata is known, with the clip duration in seconds. */
  onLoaded?: (durationSec: number) => void;
  /**
   * Current playback time in seconds (added — see the note above). Driven by
   * `requestVideoFrameCallback` when available, else `timeupdate` + rAF.
   */
  onTime?: (sec: number) => void;
  /** Fired when playback reaches the end. */
  onEnded?: () => void;
  /** Start muted (default true — required for autoplay). */
  muted?: boolean;
  className?: string;
}

// `requestVideoFrameCallback` isn't in every TS DOM lib version; describe the
// slice we use rather than depend on the ambient typing.
type VideoFrameMeta = { mediaTime?: number };
type RVFCVideo = HTMLVideoElement & {
  requestVideoFrameCallback?: (cb: (now: number, meta: VideoFrameMeta) => void) => number;
  cancelVideoFrameCallback?: (handle: number) => void;
};

type Status = "loading" | "ready" | "error";

export function GalleryVideo({
  src,
  poster,
  playing,
  seekRequest,
  onLoaded,
  onTime,
  onEnded,
  muted = true,
  className,
}: GalleryVideoProps) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [status, setStatus] = useState<Status>("loading");
  const [isMuted, setIsMuted] = useState(muted);
  // A user gesture (tap-to-play / unmute) lifts the reduced-motion and
  // autoplay-policy blocks for the rest of this clip's life.
  const [userActivated, setUserActivated] = useState(false);
  const [autoplayBlocked, setAutoplayBlocked] = useState(false);

  const reducedMotion = useReducedMotion();
  // Never auto-start under "Reduce motion" — wait for an explicit gesture.
  const blockAutoplay = reducedMotion && !userActivated;

  // Keep the latest callbacks in refs so the time loop / event listeners
  // don't re-subscribe when a parent passes fresh inline closures.
  const onTimeRef = useRef(onTime);
  const onLoadedRef = useRef(onLoaded);
  const onEndedRef = useRef(onEnded);
  useEffect(() => {
    onTimeRef.current = onTime;
    onLoadedRef.current = onLoaded;
    onEndedRef.current = onEnded;
  });

  // A seek that arrives before metadata is ready is parked here and applied
  // on `loadedmetadata`.
  const pendingSeekRef = useRef<number | null>(null);

  // Reflect the muted state onto the element imperatively — React's `muted`
  // attribute is famously unreliable at setting the DOM property.
  useEffect(() => {
    if (videoRef.current) videoRef.current.muted = isMuted;
  }, [isMuted]);

  // A new src resets the lifecycle.
  useEffect(() => {
    setStatus("loading");
    setUserActivated(false);
    setAutoplayBlocked(false);
  }, [src]);

  // Load lifecycle: loadedmetadata → ready (+ onLoaded, + flush pending seek);
  // error → honest error state; ended → onEnded.
  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;

    const handleLoadedMetadata = () => {
      setStatus("ready");
      onLoadedRef.current?.(Number.isFinite(v.duration) ? v.duration : 0);
      if (pendingSeekRef.current != null) {
        try {
          v.currentTime = pendingSeekRef.current;
        } catch {
          /* element not seekable yet; harmless */
        }
        pendingSeekRef.current = null;
      }
    };
    const handleError = () => setStatus("error");
    const handleEnded = () => onEndedRef.current?.();

    v.addEventListener("loadedmetadata", handleLoadedMetadata);
    v.addEventListener("error", handleError);
    v.addEventListener("ended", handleEnded);
    // The element may have already errored (bad src) before we subscribed.
    if (v.error) setStatus("error");

    return () => {
      v.removeEventListener("loadedmetadata", handleLoadedMetadata);
      v.removeEventListener("error", handleError);
      v.removeEventListener("ended", handleEnded);
    };
  }, [src]);

  // Time reporting → onTime. Prefer requestVideoFrameCallback (frame-accurate,
  // pauses with the video); fall back to `timeupdate` (+ rAF while playing for
  // smoother scrubbing). Only runs once ready.
  useEffect(() => {
    const v = videoRef.current as RVFCVideo | null;
    if (!v || status !== "ready") return;

    if (typeof v.requestVideoFrameCallback === "function") {
      let handle = 0;
      let cancelled = false;
      const loop = (_now: number, meta: VideoFrameMeta) => {
        if (cancelled) return;
        onTimeRef.current?.(meta.mediaTime ?? v.currentTime);
        handle = v.requestVideoFrameCallback!(loop);
      };
      handle = v.requestVideoFrameCallback(loop);
      return () => {
        cancelled = true;
        v.cancelVideoFrameCallback?.(handle);
      };
    }

    const report = () => onTimeRef.current?.(v.currentTime);
    v.addEventListener("timeupdate", report);
    let raf = 0;
    let running = true;
    const tick = () => {
      if (!running) return;
      if (!v.paused && !v.ended) report();
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => {
      running = false;
      v.removeEventListener("timeupdate", report);
      cancelAnimationFrame(raf);
    };
  }, [status]);

  // Honor `playing` once ready. play() returns a promise that rejects with
  // AbortError when a pause() interrupts it — swallow that. A blocked-autoplay
  // rejection surfaces the tap-to-play affordance rather than an error.
  useEffect(() => {
    const v = videoRef.current;
    if (!v || status !== "ready") return;
    if (playing && !blockAutoplay) {
      const p = v.play();
      if (p && typeof p.catch === "function") {
        p.catch((err: unknown) => {
          const name = (err as { name?: string } | null)?.name;
          if (name === "AbortError") return; // benign: pause() raced play()
          if (name === "NotAllowedError") {
            setAutoplayBlocked(true); // autoplay policy → surface tap-to-play
            return;
          }
          // Decode/source failures arrive on the element's `error` event,
          // which drives the error state — don't double-handle here.
        });
      }
    } else {
      v.pause();
    }
  }, [playing, status, blockAutoplay]);

  // Seek on nonce change. Apply immediately when ready, else park it.
  useEffect(() => {
    if (!seekRequest) return;
    const v = videoRef.current;
    if (v && status === "ready") {
      try {
        v.currentTime = seekRequest.sec;
      } catch {
        pendingSeekRef.current = seekRequest.sec;
      }
    } else {
      pendingSeekRef.current = seekRequest.sec;
    }
    // Intentionally keyed on nonce only: the contract is "seek when the nonce
    // changes", so re-firing the same sec with a new nonce still seeks.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seekRequest?.nonce]);

  const activate = () => {
    setUserActivated(true);
    setAutoplayBlocked(false);
    // We're inside a click → autoplay policy is satisfied; start now.
    videoRef.current?.play().catch(() => {
      /* a racing pause() AbortError, or a source error handled elsewhere */
    });
  };

  const showPlayAffordance =
    status === "ready" && playing && (blockAutoplay || autoplayBlocked);

  return (
    <div className={clsx("relative overflow-hidden bg-canvas", className)}>
      <video
        ref={videoRef}
        src={src}
        poster={poster}
        playsInline
        preload="metadata"
        className="h-full w-full object-contain"
        // No `autoPlay` attribute: play is controlled via the `playing` prop
        // so the parent's intent (and reduced-motion) stays authoritative.
      />

      {status === "loading" && (
        <div
          className="pointer-events-none absolute inset-0 flex items-center justify-center bg-canvas/60"
          aria-hidden="true"
        >
          <span className="h-6 w-6 animate-pulse rounded-full bg-ink-500" />
        </div>
      )}

      {status === "error" && (
        <div
          role="status"
          className="absolute inset-0 flex flex-col items-center justify-center gap-1 bg-canvas px-4 text-center"
        >
          <span className="text-sm font-medium text-ink-100">Clip unavailable</span>
          <span className="text-xs text-ink-400">
            The brain readout still plays below.
          </span>
        </div>
      )}

      {showPlayAffordance && (
        <button
          type="button"
          onClick={activate}
          aria-label="Play video"
          className="absolute inset-0 flex items-center justify-center bg-canvas/40 transition-colors hover:bg-canvas/30"
        >
          <span className="flex h-12 w-12 items-center justify-center rounded-full bg-ink-50/90 text-canvas">
            ▶
          </span>
        </button>
      )}

      {status === "ready" && (
        <button
          type="button"
          onClick={() => {
            setIsMuted((m) => !m);
            setUserActivated(true); // unmuting is a user gesture
          }}
          aria-label={isMuted ? "Unmute video" : "Mute video"}
          aria-pressed={!isMuted}
          className="absolute bottom-2 right-2 rounded-sm bg-canvas/70 px-2 py-1 text-xs font-medium text-ink-100 hover:bg-canvas/90"
        >
          {isMuted ? "Unmute" : "Mute"}
        </button>
      )}
    </div>
  );
}
