"use client";

import { Canvas } from "@react-three/fiber";
import { OrbitControls } from "@react-three/drei";
import {
  Component,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { REGION_IDS } from "@shared/types";
import { PlaceholderMesh } from "./PlaceholderMesh";
import { CorticalSurface, type CorticalHoverEvent } from "./CorticalSurface";
import { useActivationFrame } from "./hooks/useActivationFrame";
import { useReducedMotion } from "./hooks/useReducedMotion";
import { useRegionMask } from "./hooks/useRegionMask";
import { RegionTooltip, type RegionHoverInfo } from "./RegionTooltip";
import {
  DevOverlay,
  DevProbe,
  useDevModeEnabled,
  type DevSample,
} from "./DevOverlay";

// Public component contract — frontend-dashboard imports this by name.
// See docs/worker-briefs/brain-viz.md for the full brief.
export interface BrainMeshProps {
  // 0..1. Drives the Dashboard hero rendering as a uniform global activation.
  activation: number;
  // Detail-view per-region timeseries (CONTRACTS.md §4 keyframe_vertices).
  // Resolved against `timestamps` + `playheadSec` to drive the frame.
  keyframeVertices?: Record<string, number[]>;
  timestamps?: number[];
  playheadSec?: number;
  onReady?: () => void;
}

const SURFACE_URL = "/brain/fsaverage5.glb";

// Probes whether the real cortical surface asset is available. If so the
// renderer upgrades from the placeholder to fsaverage5. Without this guard
// useGLTF would suspend forever on 404 and there is no clean way to recover
// inside the Canvas tree.
function useSurfaceAvailable(url: string): boolean {
  const [ok, setOk] = useState(false);
  useEffect(() => {
    let cancelled = false;
    fetch(url, { method: "HEAD", cache: "force-cache" })
      .then((r) => {
        if (!cancelled && r.ok) setOk(true);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [url]);
  return ok;
}

class SurfaceErrorBoundary extends Component<
  { fallback: ReactNode; children: ReactNode },
  { errored: boolean }
> {
  state = { errored: false };
  static getDerivedStateFromError() {
    return { errored: true };
  }
  componentDidCatch(err: unknown) {
    console.warn("[brain-viz] cortical surface failed to render", err);
  }
  render() {
    return this.state.errored ? this.props.fallback : this.props.children;
  }
}

export function BrainMesh({
  activation,
  keyframeVertices,
  timestamps,
  playheadSec,
  onReady,
}: BrainMeshProps) {
  const reduceMotion = useReducedMotion();
  const surfaceAvailable = useSurfaceAvailable(SURFACE_URL);
  const regionMask = useRegionMask();
  const frame = useActivationFrame(
    activation,
    keyframeVertices,
    timestamps,
    playheadSec,
  );

  const devEnabled = useDevModeEnabled();
  const [devSample, setDevSample] = useState<DevSample | null>(null);
  const mountedAt = useRef(performance.now());
  const [firstPaintMs, setFirstPaintMs] = useState<number | null>(null);

  // Container-relative pointer position for the tooltip.
  const containerRef = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<RegionHoverInfo | null>(null);

  const handleHover = useCallback((e: CorticalHoverEvent | null) => {
    if (!e || !containerRef.current) {
      setHover(null);
      return;
    }
    const rect = containerRef.current.getBoundingClientRect();
    setHover({
      regionId: e.regionId,
      activation: e.activation,
      x: e.clientX - rect.left,
      y: e.clientY - rect.top,
    });
  }, []);

  const handleReady = useCallback(() => {
    if (firstPaintMs === null) {
      setFirstPaintMs(performance.now() - mountedAt.current);
    }
    onReady?.();
  }, [firstPaintMs, onReady]);

  return (
    <div ref={containerRef} className="relative h-full w-full">
      <Canvas
        // dpr capped to keep the FPS floor on 2021 MBP M1.
        dpr={[1, 2]}
        camera={{ position: [0, 0, 3.2], fov: 35 }}
        gl={{ antialias: true, alpha: true, powerPreference: "high-performance" }}
        frameloop={reduceMotion ? "demand" : "always"}
      >
        <color attach="background" args={["#0a0b0d"]} />
        <ambientLight intensity={0.45} />
        <directionalLight position={[2.5, 3, 2]} intensity={0.9} />
        <directionalLight position={[-2, -1, -1.5]} intensity={0.25} />

        <SurfaceErrorBoundary
          fallback={
            <PlaceholderMesh byRegion={frame.byRegion} onReady={handleReady} />
          }
        >
          <Suspense
            fallback={
              <PlaceholderMesh
                byRegion={frame.byRegion}
                onReady={handleReady}
              />
            }
          >
            {surfaceAvailable ? (
              <CorticalSurface
                url={SURFACE_URL}
                byRegion={frame.byRegion}
                vertexRegions={regionMask ?? undefined}
                regionOrder={REGION_IDS}
                onReady={handleReady}
                onHover={handleHover}
              />
            ) : (
              <PlaceholderMesh
                byRegion={frame.byRegion}
                onReady={handleReady}
              />
            )}
          </Suspense>
        </SurfaceErrorBoundary>

        <OrbitControls
          enablePan={false}
          enableZoom={false}
          enableDamping={!reduceMotion}
          dampingFactor={0.08}
          rotateSpeed={0.6}
        />

        {devEnabled && (
          <DevProbe onSample={setDevSample} firstPaintMs={firstPaintMs} />
        )}
      </Canvas>

      <RegionTooltip hover={hover} />
      {devEnabled && <DevOverlay sample={devSample} />}
    </div>
  );
}

export default BrainMesh;
