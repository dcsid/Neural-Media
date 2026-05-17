"use client";

import { useEffect, useState } from "react";
import { NUM_VERTICES } from "@shared/types";

// Per-vertex region assignment for the fsaverage5 cortical surface.
//
// One byte per vertex, value is the index into REGION_IDS (or 255 for
// "no region" — medial wall, etc.). Lives at /brain/fsaverage5.regions.bin
// because the API does not (yet) expose REGION_VERTEX_MASKS over the wire;
// the canonical source is
//   services/inference/neural_media_inference/aggregate.py::REGION_VERTEX_MASKS
// and the build script at apps/web/public/brain/README.md bakes those same
// slabs into the binary committed alongside the .glb.
//
// Cached at module scope so the fetch happens at most once per session,
// regardless of how many BrainMesh instances mount.

let cached: Uint8Array | null = null;
let inflight: Promise<Uint8Array> | null = null;

async function load(url: string): Promise<Uint8Array> {
  if (cached) return cached;
  if (inflight) return inflight;
  inflight = (async () => {
    const res = await fetch(url, { cache: "force-cache" });
    if (!res.ok) {
      inflight = null;
      throw new Error(`failed to load ${url}: ${res.status}`);
    }
    const buf = new Uint8Array(await res.arrayBuffer());
    if (buf.length !== NUM_VERTICES) {
      // Don't blow up — hover just stays disabled.
      console.warn(
        `[brain-viz] ${url} length ${buf.length} != ${NUM_VERTICES}; ignoring`,
      );
      inflight = null;
      throw new Error("region mask length mismatch");
    }
    cached = buf;
    inflight = null;
    return buf;
  })();
  return inflight;
}

export function useRegionMask(url = "/brain/fsaverage5.regions.bin"): Uint8Array | null {
  const [mask, setMask] = useState<Uint8Array | null>(cached);
  useEffect(() => {
    if (cached) {
      setMask(cached);
      return;
    }
    let cancelled = false;
    load(url)
      .then((m) => {
        if (!cancelled) setMask(m);
      })
      .catch(() => {
        // Hover stays disabled; the surface still renders.
      });
    return () => {
      cancelled = true;
    };
  }, [url]);
  return mask;
}
