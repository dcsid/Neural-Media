"use client";

import { useFrame } from "@react-three/fiber";
import { useEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import type { RegionId } from "@shared/types";
import { cividisFill } from "./lut";
import { placeholderRegionForDir } from "./regions";
import { useReducedMotion } from "./hooks/useReducedMotion";

interface PlaceholderMeshProps {
  byRegion: Record<RegionId, number>;
  onReady?: () => void;
}

// Low-poly fallback that renders before / instead of the real fsaverage
// surface. An icosahedron is squished to a brain-ish ellipsoid and lightly
// perturbed so the silhouette reads as a cortex rather than a beach ball.
// Vertex colours come from the cividis LUT applied to per-region activation.

function buildBrainGeometry(): THREE.BufferGeometry {
  const geom = new THREE.IcosahedronGeometry(1, 4);

  const pos = geom.getAttribute("position") as THREE.BufferAttribute;
  const v = new THREE.Vector3();
  const noise = (x: number, y: number, z: number) =>
    0.04 *
    (Math.sin(2.7 * x + 1.3) +
      Math.sin(3.1 * y - 0.9) * 0.7 +
      Math.sin(2.3 * z + 2.1) * 0.6);

  for (let i = 0; i < pos.count; i++) {
    v.fromBufferAttribute(pos, i).normalize();
    // Ellipsoid: longer A-P, narrower vertical.
    const ex = v.x * 1.25;
    const ey = v.y * 0.95;
    const ez = v.z * 1.05;
    const r = 1 + noise(ex, ey, ez);
    pos.setXYZ(i, ex * r, ey * r, ez * r);
  }
  pos.needsUpdate = true;
  geom.computeVertexNormals();
  return geom;
}

export function PlaceholderMesh({ byRegion, onReady }: PlaceholderMeshProps) {
  const meshRef = useRef<THREE.Mesh>(null);
  const reduceMotion = useReducedMotion();

  // Build geometry + per-vertex region assignment + color buffer once.
  const { geometry, regionPerVertex, colorAttr, scratch } = useMemo(() => {
    const g = buildBrainGeometry();
    const pos = g.getAttribute("position") as THREE.BufferAttribute;
    const count = pos.count;

    const regions = new Array<RegionId>(count);
    for (let i = 0; i < count; i++) {
      regions[i] = placeholderRegionForDir(pos.getX(i), pos.getY(i), pos.getZ(i));
    }

    const colors = new Float32Array(count * 3);
    const attr = new THREE.BufferAttribute(colors, 3);
    g.setAttribute("color", attr);

    return {
      geometry: g,
      regionPerVertex: regions,
      colorAttr: attr,
      scratch: new Float32Array(count),
    };
  }, []);

  useEffect(() => {
    return () => {
      geometry.dispose();
    };
  }, [geometry]);

  useEffect(() => {
    onReady?.();
  }, [onReady]);

  // Smoothly track region activation values so changes in `activation` glide
  // rather than pop. Under prefers-reduced-motion the smoothing collapses to
  // an instant snap.
  const displayed = useRef<Record<RegionId, number>>({ ...byRegion });

  useFrame((_, delta) => {
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
      for (let i = 0; i < regionPerVertex.length; i++) {
        scratch[i] = displayed.current[regionPerVertex[i]] ?? 0;
      }
      cividisFill(scratch, colorAttr.array as Float32Array);
      colorAttr.needsUpdate = true;
    }

    if (meshRef.current && !reduceMotion) {
      meshRef.current.rotation.y += delta * 0.18;
    }
  });

  // Initial paint — fill colours once before the first useFrame runs.
  useEffect(() => {
    for (let i = 0; i < regionPerVertex.length; i++) {
      scratch[i] = displayed.current[regionPerVertex[i]] ?? 0;
    }
    cividisFill(scratch, colorAttr.array as Float32Array);
    colorAttr.needsUpdate = true;
  }, [regionPerVertex, colorAttr, scratch]);

  return (
    <mesh ref={meshRef} geometry={geometry} castShadow={false} receiveShadow={false}>
      <meshStandardMaterial
        vertexColors
        roughness={0.55}
        metalness={0.05}
        flatShading={false}
      />
    </mesh>
  );
}
