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
  regionId: RegionId;
  // The predicted activation value at the hovered vertex. For per-vertex
  // mode this is `perVertex[vertexIndex]`; otherwise it is `byRegion[r]`.
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
  onReady?: () => void;
  onHover?: (e: CorticalHoverEvent | null) => void;
}

export function CorticalSurface({
  url,
  byRegion,
  perVertex,
  vertexRegions,
  regionOrder,
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

  // Surface state changed — re-render. In demand frameloop mode (reduce-
  // motion path) this is required for new colours to ever land; in always
  // mode it's a no-op.
  useEffect(() => {
    invalidate();
  }, [byRegion, perVertex, invalidate]);

  const displayed = useRef<Record<RegionId, number>>({ ...byRegion });

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

  useFrame((_, delta) => {
    const count = scratch.length;

    if (perVertex && perVertex.length === count) {
      cividisFill(perVertex, colorAttr.array as Float32Array);
      colorAttr.needsUpdate = true;
    } else if (vertexRegions && regionOrder && vertexRegions.length === count) {
      const ease = reduceMotion ? 1 : 1 - Math.exp(-delta * 6);
      let dirty = false;
      for (const r in byRegion) {
        const key = r as RegionId;
        const target = byRegion[key];
        const cur = displayed.current[key] ?? target;
        const next = cur + (target - cur) * ease;
        if (Math.abs(next - cur) > 1e-4) dirty = true;
        displayed.current[key] = next;
      }
      if (dirty) {
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
        colorAttr.needsUpdate = true;
        if (reduceMotion) invalidate();
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
      colorAttr.needsUpdate = true;
    }

    if (groupRef.current && !reduceMotion) {
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
      // Suppress the tooltip in that case rather than show a stand-in
      // value: we have no calibrated activation for those vertices and
      // a "—" readout would imply we do.
      if (regionIdx === 255) {
        onHover(null);
        return;
      }
      const region = regionOrder[regionIdx];
      const activation = perVertex
        ? (perVertex[best] ?? 0)
        : (byRegion[region] ?? 0);
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
