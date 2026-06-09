"use client";

// TEMPORARY stub of T3's <GalleryVideo> (components/gallery/GalleryVideo.tsx).
// Implements the same controlled API so the gallery sync wiring can land + be
// reviewed before T3's component exists. When T3 lands, delete this file and
// switch the import in app/gallery/page.tsx to:
//     import { GalleryVideo, type GalleryVideoProps } from "@/components/gallery/GalleryVideo";
//
// Controlled contract (do NOT add props beyond this — page builds against it):
//   { src, poster?, playing, seekRequest?{sec,nonce}, onTime(sec),
//     onLoaded?(dur), onEnded?, muted?, className? }

import { useEffect, useRef } from "react";

export interface GalleryVideoProps {
  src: string;
  poster?: string;
  playing: boolean;
  // Bump `nonce` to force a seek even when `sec` is unchanged (e.g. re-clicking
  // the same spot). The parent owns the playhead; this is a one-shot request.
  seekRequest?: { sec: number; nonce: number };
  onTime: (sec: number) => void;
  onLoaded?: (durationSec: number) => void;
  onEnded?: () => void;
  muted?: boolean;
  className?: string;
}

export function GalleryVideo({
  src,
  poster,
  playing,
  seekRequest,
  onTime,
  onLoaded,
  onEnded,
  muted = true,
  className,
}: GalleryVideoProps) {
  const ref = useRef<HTMLVideoElement>(null);
  const lastNonce = useRef<number | null>(null);

  // Drive native play/pause from the controlled `playing` prop. play() can
  // reject (autoplay policy / not-yet-loaded) — swallow it; the next user
  // gesture retries.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (playing) {
      void el.play().catch(() => {});
    } else {
      el.pause();
    }
  }, [playing]);

  // Apply a one-shot seek request. Guard on the nonce so we only seek when the
  // parent issues a new request — never on every render.
  useEffect(() => {
    const el = ref.current;
    if (!el || !seekRequest) return;
    if (lastNonce.current === seekRequest.nonce) return;
    lastNonce.current = seekRequest.nonce;
    const sec = seekRequest.sec;
    if (Number.isFinite(sec)) el.currentTime = Math.max(0, sec);
  }, [seekRequest]);

  return (
    <video
      ref={ref}
      src={src}
      poster={poster}
      muted={muted}
      playsInline
      preload="metadata"
      className={className}
      onTimeUpdate={(e) => onTime(e.currentTarget.currentTime)}
      onLoadedMetadata={(e) => onLoaded?.(e.currentTarget.duration)}
      onEnded={() => onEnded?.()}
    />
  );
}

export default GalleryVideo;
