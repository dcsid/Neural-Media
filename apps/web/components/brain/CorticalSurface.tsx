"use client";

import { useGLTF } from "@react-three/drei";
import { useFrame, useThree, type ThreeEvent } from "@react-three/fiber";
import { useCallback, useEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import { NUM_VERTICES, type RegionId } from "@shared/types";
import { cividisFill } from "./lut";
import { useReducedMotion } from "./hooks/useReducedMotion";

// Loaded cortical surface (fsaverage5, 20,484 vertices, both hemispheres
// concatenated left-then-right to match the TRIBE per-vertex column order).
// See apps/web/public/brain/README.md for the build pipeline.

export interface CorticalHoverEvent {
  vertexIndex: number;
  // `null` when the hovered vertex falls on the unassigned medial wall
  // / off-atlas territory (sentinel 255 in the .regions.bin convention).
  // Consumers should render this as "(unassigned)" rather than treat
  // null as "no hover".
  regionId: RegionId | null;
  // The predicted activation value at the hovered vertex. For per-vertex
  // mode this is `perVertex[vertexIndex]`; otherwise it is `byRegion[r]`.
  // 0 when the vertex is unassigned (we have no calibrated value).
  activation: number;
  clientX: number;
  clientY: number;
}

interface CorticalSurfaceProps {
  url: string;
  byRegion: Record<RegionId, number>;
  // Optional per-vertex activation override. If provided, takes precedence
  // over `byRegion` (Detail mode with full vertex resolution).
  perVertex?: Float32Array;
  // Per-vertex region assignment for fsaverage5. Length === NUM_VERTICES.
  vertexRegions?: Uint8Array;
  // REGION_IDS order — the byte value in vertexRegions indexes into this.
  regionOrder?: ReadonlyArray<RegionId>;
  // Tour-mode spotlight: when set, boost the brightness of vertices in
  // this region and dim everything else. Smoothed via the same easing
  // pattern as the per-region colour writer so toggling it doesn't pop.
  // null / undefined means "no spotlight" (the default, full brightness).
  highlightRegion?: RegionId | null;
  // If true, dampen the highlight boost (1.1× / 0.85×) for reduced-motion
  // mode. Default false.
  highlightDamped?: boolean;
  // Disable the surface's idle auto-rotation. The tour drives the camera
  // externally and the surface group rotation would compound with it,
  // making the sweep feel jittery. Default false (rotation on).
  freezeRotation?: boolean;
  // Current scrubber playhead. Used purely to detect "is playing right now"
  // by watching for monotonic increases between frames — when the playhead
  // is advancing, the surface applies a subtle low-frequency pulse
  // proportional to each region's mean activation. Skipped entirely under
  // prefers-reduced-motion.
  playheadSec?: number;
  onReady?: () => void;
  onHover?: (e: CorticalHoverEvent | null) => void;
}

export function CorticalSurface({
  url,
  byRegion,
  perVertex,
  vertexRegions,
  regionOrder,
  highlightRegion = null,
  highlightDamped = false,
  freezeRotation = false,
  playheadSec,
  onReady,
  onHover,
}: CorticalSurfaceProps) {
  const gltf = useGLTF(url) as unknown as { scene: THREE.Group };
  const reduceMotion = useReducedMotion();
  const groupRef = useRef<THREE.Group>(null);
  const invalidate = useThree((s) => s.invalidate);

  const { mesh, colorAttr, scratch } = useMemo(() => {
    let found: THREE.Mesh | null = null;
    gltf.scene.traverse((obj) => {
      if (!found && (obj as THREE.Mesh).isMesh) found = obj as THREE.Mesh;
    });
    if (!found) {
      throw new Error(
        `BrainMesh: ${url} did not contain a mesh. Expected fsaverage5 cortical surface.`,
      );
    }
    const m = found as THREE.Mesh;
    const geo = m.geometry as THREE.BufferGeometry;
    const pos = geo.getAttribute("position") as THREE.BufferAttribute;
    const count = pos.count;

    let attr = geo.getAttribute("color") as THREE.BufferAttribute | undefined;
    if (!attr) {
      attr = new THREE.BufferAttribute(new Float32Array(count * 3), 3);
      geo.setAttribute("color", attr);
    }

    const mat = new THREE.MeshStandardMaterial({
      vertexColors: true,
      roughness: 0.55,
      metalness: 0.05,
    });
    m.material = mat;

    return { mesh: m, colorAttr: attr, scratch: new Float32Array(count) };
  }, [gltf, url]);

  useEffect(() => {
    onReady?.();
  }, [onReady]);

  // When rotation is frozen externally (tour mode drives the camera),
  // reset the surface group's accumulated y-rotation to 0 so the tour
  // camera azimuth defines the only visible orientation. Snapping is
  // fine here — the tour eases in from t=0 anyway.
  useEffect(() => {
    if (freezeRotation && groupRef.current) {
      groupRef.current.rotation.y = 0;
      invalidate();
    }
  }, [freezeRotation, invalidate]);

  // Surface state changed — re-render. In demand frameloop mode (reduce-
  // motion path) this is required for new colours to ever land; in always
  // mode it's a no-op.
  useEffect(() => {
    invalidate();
  }, [byRegion, perVertex, invalidate]);

  const displayed = useRef<Record<RegionId, number>>({ ...byRegion });

  // Highlight strength is a single scalar in [0, 1] that we smoothly
  // animate toward the target (1 when a region is spotlit, 0 when not).
  // We pair it with the currently-displayed region so a switch-from-one-
  // region-to-another fades through 0 rather than swapping abruptly.
  const highlightStrength = useRef(0);
  const highlightTargetRegion = useRef<RegionId | null>(null);
  highlightTargetRegion.current = highlightRegion;

  // Pulse detection: track when the playhead last advanced. If a forward
  // step happened in the last ~500ms we treat the scrubber as "playing"
  // and apply the low-frequency region pulse below. Refs only — no state
  // churn, no extra renders. Initial value `null` so a first paint with
  // playheadSec===0 doesn't trip the change detector.
  const lastPlayheadRef = useRef<number | null>(null);
  const lastForwardAtRef = useRef<number>(0);
  if (playheadSec !== undefined) {
    const prev = lastPlayheadRef.current;
    if (prev === null) {
      lastPlayheadRef.current = playheadSec;
    } else if (playheadSec > prev + 1e-4) {
      lastForwardAtRef.current = performance.now();
      lastPlayheadRef.current = playheadSec;
    } else if (playheadSec < prev - 1e-4) {
      // Backward seek: still consider "moving" so the pulse stays alive
      // briefly, but don't extend the playing window indefinitely.
      lastPlayheadRef.current = playheadSec;
    }
  }
  // Phase accumulator for the pulse; advanced inside useFrame so the
  // oscillation is independent of frame rate.
  const pulsePhaseRef = useRef(0);
  // Smoothed pulse intensity in [0, 1] — eases up when playing, eases
  // down when paused. Avoids a hard on/off pop at the 500ms boundary.
  const pulseEnvelopeRef = useRef(0);

  // First-paint colour fill. Without this, the useFrame dirty-check would
  // see displayed === target and skip the paint on frame 1, leaving the
  // zero-initialised colour buffer = cividis(0) on every vertex. This
  // matters most under prefers-reduced-motion (frameloop=demand), where
  // the next frame may not run until the user actually interacts.
  useEffect(() => {
    const count = scratch.length;
    if (perVertex && perVertex.length === count) {
      cividisFill(perVertex, colorAttr.array as Float32Array);
    } else if (vertexRegions && regionOrder && vertexRegions.length === count) {
      for (let i = 0; i < count; i++) {
        const idx = vertexRegions[i];
        if (idx === 255) {
          scratch[i] = 0;
          continue;
        }
        const r = regionOrder[idx];
        scratch[i] = displayed.current[r] ?? 0;
      }
      cividisFill(scratch, colorAttr.array as Float32Array);
    } else {
      let sum = 0;
      let n = 0;
      for (const r in byRegion) {
        sum += byRegion[r as RegionId];
        n += 1;
      }
      const g = n > 0 ? sum / n : 0;
      for (let i = 0; i < count; i++) scratch[i] = g;
      cividisFill(scratch, colorAttr.array as Float32Array);
    }
    colorAttr.needsUpdate = true;
    invalidate();
    // Intentional dep set: we only want this on mount and when the region
    // mask becomes available. Per-frame updates flow through useFrame.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [vertexRegions, regionOrder, perVertex]);

  // Highlight boost / dim factors. Damped variant is used for prefers-
  // reduced-motion: the spotlight still happens (so the user can see
  // which region is being labelled) but the intensity swing is small.
  const HIGHLIGHT_BOOST = highlightDamped ? 1.1 : 1.4;
  const HIGHLIGHT_DIM = highlightDamped ? 0.85 : 0.7;

  // Region pulse parameters. Frequency capped at the upper end of "barely
  // visible cadence" so the surface reads as alive without flashing —
  // 0.9 Hz puts us comfortably below the photosensitivity threshold of
  // 3 Hz / WCAG 2.3.1. Amplitude is small enough to read as ambient
  // breathing on the surface even on regions at peak activation.
  const PULSE_HZ = 0.9;
  const PULSE_BASE_AMP = 0.07;
  // Envelope decay constants: rise fast on a new playhead step, fall
  // gently when the scrubber pauses. Both eased so the pulse doesn't
  // pop in or out on toggle.
  const PULSE_RISE_RATE = 8;   // ≈ 125ms to mostly on
  const PULSE_FALL_RATE = 2.5; // ≈ 400ms to mostly off

  useFrame((_, delta) => {
    const count = scratch.length;
    const ease = reduceMotion ? 1 : 1 - Math.exp(-delta * 6);

    // Pulse bookkeeping. Drive the envelope toward 1 when the playhead
    // moved recently, otherwise toward 0. Reduced-motion users get 0,
    // always — the surface stays still for them. Advance the phase
    // regardless so the pulse picks up mid-cycle on resume.
    const nowMs = performance.now();
    const recentlyForward = nowMs - lastForwardAtRef.current < 500;
    const pulseTarget = !reduceMotion && recentlyForward ? 1 : 0;
    const envRate =
      pulseTarget > pulseEnvelopeRef.current ? PULSE_RISE_RATE : PULSE_FALL_RATE;
    pulseEnvelopeRef.current +=
      (pulseTarget - pulseEnvelopeRef.current) * (1 - Math.exp(-delta * envRate));
    pulsePhaseRef.current += delta * 2 * Math.PI * PULSE_HZ;
    if (pulsePhaseRef.current > 1e6) pulsePhaseRef.current = 0;
    const pulseAmp = PULSE_BASE_AMP * pulseEnvelopeRef.current;
    const pulseActive = pulseAmp > 1e-3;

    // Drive the highlight scalar toward 1 when a region is target and 0
    // when none is. We snapshot the previous value so we can detect
    // whether we need to repaint when the colour writer would otherwise
    // skip (dirty=false).
    const prevHighlight = highlightStrength.current;
    const highlightTarget =
      highlightTargetRegion.current !== null ? 1 : 0;
    const nextHighlight =
      prevHighlight + (highlightTarget - prevHighlight) * ease;
    highlightStrength.current = nextHighlight;
    const highlightChanged = Math.abs(nextHighlight - prevHighlight) > 1e-3;

    // Render the base colours into colorAttr.array.
    let didRepaint = false;
    if (perVertex && perVertex.length === count) {
      cividisFill(perVertex, colorAttr.array as Float32Array);
      didRepaint = true;
    } else if (vertexRegions && regionOrder && vertexRegions.length === count) {
      let dirty = false;
      for (const r in byRegion) {
        const key = r as RegionId;
        const target = byRegion[key];
        const cur = displayed.current[key] ?? target;
        const next = cur + (target - cur) * ease;
        if (Math.abs(next - cur) > 1e-4) dirty = true;
        displayed.current[key] = next;
      }
      // Always repaint while a highlight transition or pulse is in flight —
      // the multiplicative modulation below needs a fresh base every frame.
      if (dirty || highlightChanged || nextHighlight > 1e-3 || pulseActive) {
        for (let i = 0; i < count; i++) {
          const idx = vertexRegions[i];
          if (idx === 255) {
            scratch[i] = 0;
            continue;
          }
          const r = regionOrder[idx];
          scratch[i] = displayed.current[r] ?? 0;
        }
        cividisFill(scratch, colorAttr.array as Float32Array);
        didRepaint = true;
      }
    } else {
      let sum = 0;
      let n = 0;
      for (const r in byRegion) {
        sum += byRegion[r as RegionId];
        n += 1;
      }
      const g = n > 0 ? sum / n : 0;
      for (let i = 0; i < count; i++) scratch[i] = g;
      cividisFill(scratch, colorAttr.array as Float32Array);
      didRepaint = true;
    }

    // Apply the spotlight multiplier. Only worth doing if the highlight
    // is visibly nonzero AND we have a region mask to know who's in/out.
    if (
      didRepaint &&
      nextHighlight > 1e-3 &&
      vertexRegions &&
      regionOrder &&
      vertexRegions.length === count
    ) {
      const region = highlightTargetRegion.current;
      const targetIdx = region ? regionOrder.indexOf(region) : -1;
      const boost = 1 + (HIGHLIGHT_BOOST - 1) * nextHighlight;
      const dim = 1 + (HIGHLIGHT_DIM - 1) * nextHighlight;
      const arr = colorAttr.array as Float32Array;
      // targetIdx === -1 means the spotlight region isn't in our
      // regionOrder; treat all vertices as dimmed in that case rather
      // than as boosted, so we never silently brighten the whole brain.
      for (let i = 0; i < count; i++) {
        const idx = vertexRegions[i];
        const f = idx === targetIdx ? boost : dim;
        const o = i * 3;
        arr[o] = Math.min(1, arr[o] * f);
        arr[o + 1] = Math.min(1, arr[o + 1] * f);
        arr[o + 2] = Math.min(1, arr[o + 2] * f);
      }
    }

    // Region pulse: subtle low-frequency brightness modulation per
    // region, scaled by that region's current mean activation. Each
    // region gets a slightly offset phase so the eight surfaces don't
    // bloom in unison — the effect reads as breathing rather than
    // strobing. Only runs when we have a region mask; the perVertex-
    // and-no-mask paths simply skip it (the visual fallback there is
    // the base cividis fill, which is already informative).
    if (
      didRepaint &&
      pulseActive &&
      vertexRegions &&
      regionOrder &&
      vertexRegions.length === count
    ) {
      const phase = pulsePhaseRef.current;
      const arr = colorAttr.array as Float32Array;
      // Precompute the per-region multiplier so the hot loop is just an
      // index lookup + three mults. Phase offsets pulled from a small
      // golden-ratio sequence so neighbouring regions never collide.
      const regionMult = new Float32Array(regionOrder.length);
      for (let r = 0; r < regionOrder.length; r++) {
        const region = regionOrder[r];
        const mean = displayed.current[region] ?? 0;
        const safeMean = Number.isFinite(mean) ? mean : 0;
        const off = r * 0.7853981633974483; // π/4 step
        regionMult[r] = 1 + pulseAmp * safeMean * Math.sin(phase + off);
      }
      for (let i = 0; i < count; i++) {
        const idx = vertexRegions[i];
        if (idx === 255) continue;
        const f = regionMult[idx] ?? 1;
        const o = i * 3;
        arr[o] = Math.min(1, arr[o] * f);
        arr[o + 1] = Math.min(1, arr[o + 1] * f);
        arr[o + 2] = Math.min(1, arr[o + 2] * f);
      }
    }

    // Pulse-only repaints need to commit too. Without this, when only
    // pulseActive is true (no dirty/highlight change) the modulation
    // we just wrote to colorAttr.array wouldn't make it to the GPU.
    if (pulseActive && !didRepaint && vertexRegions && regionOrder) {
      colorAttr.needsUpdate = true;
    }

    if (didRepaint) {
      colorAttr.needsUpdate = true;
      if (reduceMotion) invalidate();
    }

    if (groupRef.current && !reduceMotion && !freezeRotation) {
      groupRef.current.rotation.y += delta * 0.12;
    }
  });

  // Hover raycast. R3F gives us face indices and the world-space hit point;
  // we transform back to local space and pick the closest of the three
  // triangle vertices. The closest-vertex approximation is fine for hover
  // readouts and avoids per-pixel barycentric work.
  const tmpVec = useRef(new THREE.Vector3()).current;
  const tmpPoint = useRef(new THREE.Vector3()).current;

  const handlePointerMove = useCallback(
    (e: ThreeEvent<PointerEvent>) => {
      if (!onHover || !vertexRegions || !regionOrder || !e.face) return;
      e.stopPropagation();
      const { a, b, c } = e.face;
      tmpPoint.copy(e.point);
      mesh.worldToLocal(tmpPoint);
      const posAttr = mesh.geometry.getAttribute(
        "position",
      ) as THREE.BufferAttribute;

      let best = a;
      let bestDist = Infinity;
      for (const i of [a, b, c]) {
        tmpVec.fromBufferAttribute(posAttr, i);
        const d = tmpVec.distanceToSquared(tmpPoint);
        if (d < bestDist) {
          bestDist = d;
          best = i;
        }
      }

      const regionIdx = vertexRegions[best];
      // 255 = "unassigned" in the .regions.bin convention (see
      // build_regions_bin.py:UNASSIGNED). HCP-MMP1 only covers ~18.6%
      // of cortical vertices — the eight canonical TRIBE regions — so
      // roughly four hovers in five land outside any curated parcel.
      // Emit a hover event with regionId=null so the tooltip can show
      // "(unassigned)" rather than disappearing on us; suppressing it
      // entirely (the old behaviour) left the user with no feedback
      // that their hover registered at all.
      if (regionIdx === 255) {
        onHover({
          vertexIndex: best,
          regionId: null,
          activation: 0,
          clientX: e.clientX,
          clientY: e.clientY,
        });
        return;
      }
      const region = regionOrder[regionIdx];
      const rawActivation = perVertex
        ? (perVertex[best] ?? 0)
        : (byRegion[region] ?? 0);
      // Guard against NaN / Infinity sneaking in from stale activation
      // buffers — `?? 0` doesn't catch NaN. The tooltip renders "—"
      // for non-finite values which is friendlier than "NaN".
      const activation = Number.isFinite(rawActivation) ? rawActivation : 0;
      onHover({
        vertexIndex: best,
        regionId: region,
        activation,
        clientX: e.clientX,
        clientY: e.clientY,
      });
    },
    [onHover, vertexRegions, regionOrder, mesh, perVertex, byRegion, tmpVec, tmpPoint],
  );

  const handlePointerOut = useCallback(
    (e: ThreeEvent<PointerEvent>) => {
      if (!onHover) return;
      // Only clear when the pointer actually leaves the mesh; R3F also
      // fires this on the first frame as the pointer crosses between
      // child intersects, but stopPropagation in `move` keeps us authoritative.
      e.stopPropagation();
      onHover(null);
    },
    [onHover],
  );

  return (
    <group ref={groupRef}>
      <primitive
        object={mesh}
        onPointerMove={handlePointerMove}
        onPointerOut={handlePointerOut}
      />
    </group>
  );
}

export function preloadCorticalSurface(url: string) {
  useGLTF.preload(url);
}

export const EXPECTED_VERTEX_COUNT = NUM_VERTICES;
