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
import { RegionLegend } from "./RegionLegend";
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
  // Set to true when /api/v1/videos/{id}/activation returned 404 because
  // the per-vertex .npz was purged after region_metrics were aggregated
  // (a tier-b cleanup path). The component keeps rendering — the
  // placeholder/cortical surface still shows, region readings still work
  // upstream — and overlays a banner so the user knows why the
  // per-vertex resolution is missing.
  activationPurged?: boolean;
  // Hide the region legend overlay. Default false (legend shown). Set to
  // true on the Dashboard hero where the surrounding chrome already
  // labels the regions and the legend would compete for attention.
  hideLegend?: boolean;
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
  activationPurged = false,
  hideLegend = false,
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
    regionMask,
    REGION_IDS,
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
      vertexIndex: e.vertexIndex,
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
            {activationPurged ? (
              // Per-vertex data was purged after region aggregation. The
              // brief calls for the low-poly placeholder in this state —
              // it's an honest signal that vertex resolution is gone
              // while region readings (driven by byRegion) keep working.
              <PlaceholderMesh
                byRegion={frame.byRegion}
                onReady={handleReady}
              />
            ) : surfaceAvailable ? (
              <CorticalSurface
                url={SURFACE_URL}
                byRegion={frame.byRegion}
                perVertex={frame.perVertex ?? undefined}
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

      {/* Show the legend on the Detail view (per-region data resolved from
          keyframes) but not on the Dashboard hero, where every region is
          painted with the same uniform scalar and a per-region readout
          would be misleading. Callers can force it off with hideLegend. */}
      {!hideLegend && keyframeVertices !== undefined && (
        <RegionLegend byRegion={frame.byRegion} />
      )}
      <RegionTooltip hover={hover} />
      {activationPurged && (
        <div
          role="status"
          className={[
            "pointer-events-none absolute inset-x-3 bottom-3 z-10",
            "border border-line bg-surface/95 px-3 py-2 backdrop-blur-sm",
            "text-[11px] leading-snug text-ink-200 shadow-sm",
          ].join(" ")}
        >
          <span className="mr-1.5 font-mono uppercase tracking-[0.08em] text-ink-300">
            Aggregated only
          </span>
          Per-vertex data was purged after aggregation. The region readings
          below still reflect the full inference run.
        </div>
      )}
      {devEnabled && <DevOverlay sample={devSample} />}
    </div>
  );
}

export default BrainMesh;
