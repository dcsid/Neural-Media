"use client";

import { useGLTF } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import { NUM_VERTICES, type RegionId } from "@shared/types";
import { cividisFill } from "./lut";
import { useReducedMotion } from "./hooks/useReducedMotion";

interface CorticalSurfaceProps {
  url: string;
  byRegion: Record<RegionId, number>;
  // Optional per-vertex activation override. If provided, takes precedence
  // over `byRegion` (Detail mode with full vertex resolution).
  perVertex?: Float32Array;
  // Per-vertex region assignment for fsaverage5. Length === NUM_VERTICES.
  // Required to colour the surface from `byRegion`. When absent we fall
  // back to a uniform global activation (avg of byRegion).
  vertexRegions?: Uint8Array;
  regionOrder?: ReadonlyArray<RegionId>;
  onReady?: () => void;
}

// Loads a pre-baked cortical surface (fsaverage5, 20,484 vertices — see
// CONTRACTS.md §4) from a GLB sitting under apps/web/public/brain/.
//
// The GLB is intentionally not committed. Drop the file in once
// ml-inference confirms the exact mesh TRIBE evaluates on:
//   apps/web/public/brain/fsaverage5.glb
//   apps/web/public/brain/fsaverage5.regions.bin   (uint8, len=20484)
// See apps/web/public/brain/README.md.

export function CorticalSurface({
  url,
  byRegion,
  perVertex,
  vertexRegions,
  regionOrder,
  onReady,
}: CorticalSurfaceProps) {
  const gltf = useGLTF(url) as unknown as {
    scene: THREE.Group;
  };
  const reduceMotion = useReducedMotion();
  const groupRef = useRef<THREE.Group>(null);

  // Extract the first mesh; flatten transforms. We expect exactly one mesh
  // covering both hemispheres (left + right concatenated, fsaverage5).
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

  const displayed = useRef<Record<RegionId, number>>({ ...byRegion });

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
          const r = regionOrder[vertexRegions[i]];
          scratch[i] = displayed.current[r] ?? 0;
        }
        cividisFill(scratch, colorAttr.array as Float32Array);
        colorAttr.needsUpdate = true;
      }
    } else {
      // No region mask yet — fall back to a uniform global activation.
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

  return (
    <group ref={groupRef}>
      <primitive object={mesh} />
    </group>
  );
}

// Hint useGLTF to clean up when route changes.
export function preloadCorticalSurface(url: string) {
  useGLTF.preload(url);
}

// Asserted at runtime by CorticalSurface's contract — the GLB must contain
// a mesh whose position attribute has length NUM_VERTICES * 3.
export const EXPECTED_VERTEX_COUNT = NUM_VERTICES;
