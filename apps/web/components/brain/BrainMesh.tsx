"use client";

import { Canvas } from "@react-three/fiber";
import { OrbitControls } from "@react-three/drei";
import {
  Component,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { REGION_DESCRIPTIONS, REGION_IDS } from "@shared/types";
import { PlaceholderMesh } from "./PlaceholderMesh";
import { CorticalSurface, type CorticalHoverEvent } from "./CorticalSurface";
import { useActivationFrame } from "./hooks/useActivationFrame";
import { useReducedMotion } from "./hooks/useReducedMotion";
import { useRegionMask } from "./hooks/useRegionMask";
import { useTour } from "./hooks/useTour";
import { useWebGLAvailable } from "./hooks/useWebGLAvailable";
import {
  BrainCanvasSkeleton,
  BrainCanvasUnavailable,
} from "./BrainCanvasStates";
import { RegionLegend } from "./RegionLegend";
import { RegionTooltip, type RegionHoverInfo } from "./RegionTooltip";
import { TourCameraDriver } from "./TourCameraDriver";
import {
  CameraPresetDriver,
  type CameraPreset,
} from "./CameraPresetDriver";
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
  // Set to true when the prediction carries only region-level series and no
  // per-vertex keyframes (the wire payload is downsampled — it never ships
  // the full 20k-vertex array). Visual consequence: BrainMesh renders the
  // low-poly **placeholder** mesh (coloured per-region from `byRegion`)
  // INSTEAD of the per-vertex cortical surface — an honest signal that
  // vertex resolution is gone — and overlays an "Aggregated only" banner
  // explaining why. Region-level readings (legend, hover) keep working
  // because they're driven by byRegion, not the purged per-vertex buffer.
  activationPurged?: boolean;
  // Hide the region legend overlay. Default false (legend shown). Set to
  // true on the Dashboard hero where the surrounding chrome already
  // labels the regions and the legend would compete for attention.
  hideLegend?: boolean;
  onReady?: () => void;
}

const SURFACE_URL = "/brain/fsaverage5.glb";

// Camera preset button definitions. Labels are short (3 chars max) to
// keep the bar compact; titles carry the full neuroscience-poster name
// for hover discoverability and screen-reader accessibility.
const CAMERA_PRESET_BUTTONS: ReadonlyArray<{
  preset: CameraPreset;
  label: string;
  title: string;
}> = [
  { preset: "lateral-l", label: "L", title: "Lateral L" },
  { preset: "lateral-r", label: "R", title: "Lateral R" },
  { preset: "medial-l", label: "M-L", title: "Medial L" },
  { preset: "medial-r", label: "M-R", title: "Medial R" },
  { preset: "dorsal", label: "Dor", title: "Dorsal (top)" },
  { preset: "ventral", label: "Ven", title: "Ventral (bottom)" },
];

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
  const webglAvailable = useWebGLAvailable();
  const surfaceAvailable = useSurfaceAvailable(SURFACE_URL);
  const regionMask = useRegionMask();

  // Tour state. Owned here so both the in-Canvas camera driver and the
  // DOM-side Tour button can read/write the same instance. The tour
  // takes over the playhead while active — see the override below
  // feeding useActivationFrame — and writes its own camera transform
  // via TourCameraDriver. Gated on `hasKeyframes` so the Dashboard hero
  // (no per-region data) never gets a "Tour" button that wouldn't do
  // anything meaningful. `totalDurationSec` falls back to a small
  // sentinel when timestamps aren't available; the tour still runs but
  // the playhead stays parked at 0 in that case.
  // Real keyframe data means at least one series with actual samples — not
  // merely a non-empty object. A map of empty arrays (e.g. `{v1: [], …}`
  // from a producer that emitted regions but no frames) would otherwise
  // light up the Tour button, the scrubber-driven legend, and the camera
  // presets with nothing behind them. Validate the series shape + a minimum
  // length so those affordances only appear when they'd actually do
  // something.
  const hasKeyframes = useMemo(() => {
    if (keyframeVertices === undefined) return false;
    return Object.values(keyframeVertices).some(
      (series) => Array.isArray(series) && series.length > 0,
    );
  }, [keyframeVertices]);
  const totalDurationSec = useMemo(() => {
    if (!timestamps || timestamps.length === 0) return 0;
    return timestamps[timestamps.length - 1] ?? 0;
  }, [timestamps]);
  const tour = useTour({
    totalDurationSec,
    enabled: hasKeyframes,
  });

  // Override the playhead when the tour is running. We don't write back
  // to a parent setter — BrainMesh just consumes its own override
  // locally. Once the tour ends, playheadSec returns to whatever the
  // parent's TimelineScrubber is feeding in, which is the desired
  // behavior: the scrub bar reflects user time again, not tour time.
  const effectivePlayheadSec = tour.active
    ? tour.frame.playheadSec
    : playheadSec;

  const frame = useActivationFrame(
    activation,
    keyframeVertices,
    timestamps,
    effectivePlayheadSec,
    regionMask,
    REGION_IDS,
  );

  // OrbitControls ref so TourCameraDriver can disable user input while
  // the tour runs and call .update() after writing the camera position.
  // The drei OrbitControls forwards the underlying three-stdlib instance;
  // we type via React.ComponentRef so we don't need a direct three-stdlib
  // import (which isn't a hoisted dep of apps/web).
  const orbitControlsRef = useRef<React.ComponentRef<typeof OrbitControls>>(null);

  // Camera preset state. requestId increments on every click so the
  // driver can re-snap even if the same preset is selected twice.
  // We don't read the preset off the URL — these are intentionally
  // ephemeral, only-as-long-as-this-mount controls.
  const [cameraPreset, setCameraPreset] = useState<CameraPreset | null>(null);
  const [cameraRequestId, setCameraRequestId] = useState(0);
  const requestPreset = useCallback((preset: CameraPreset) => {
    setCameraPreset(preset);
    setCameraRequestId((n) => n + 1);
  }, []);

  const devEnabled = useDevModeEnabled();
  const [devSample, setDevSample] = useState<DevSample | null>(null);
  const mountedAt = useRef(performance.now());
  const [firstPaintMs, setFirstPaintMs] = useState<number | null>(null);
  // Gates the loading skeleton overlay — flips true on the first painted
  // frame (handleReady) so the canvas box never flashes empty.
  const [ready, setReady] = useState(false);

  // Container-relative pointer position for the tooltip.
  const containerRef = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<RegionHoverInfo | null>(null);

  const handleHover = useCallback((e: CorticalHoverEvent | null) => {
    // Translate the surface's clientX/Y hover into container-relative
    // coordinates for the tooltip; clear it when the pointer leaves.
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
    setReady(true);
    if (firstPaintMs === null) {
      setFirstPaintMs(performance.now() - mountedAt.current);
    }
    onReady?.();
  }, [firstPaintMs, onReady]);

  // Cancellation wiring: Escape key cancels from anywhere on the page;
  // a pointer-down anywhere inside the Canvas wrapper (including drags
  // on the mesh, which would otherwise be eaten by OrbitControls when
  // re-enabled) also cancels. We attach the pointer handler at the
  // container level rather than to OrbitControls itself so it fires
  // even though controls.enabled is false during the tour.
  useEffect(() => {
    if (!tour.active) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") tour.cancel();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [tour.active, tour.cancel, tour]);

  const onCanvasPointerDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (!tour.active) return;
      // Defensive: never let a Tour button click bubble into a cancel.
      // The button stops propagation explicitly, but defending here too
      // keeps the contract local. data-tour-button is added to the
      // button below.
      const target = e.target as HTMLElement | null;
      if (target?.closest("[data-tour-button]")) return;
      tour.cancel();
    },
    [tour],
  );

  // No WebGL → render an honest static fallback instead of mounting a Canvas
  // that would throw on context creation (the surface error boundary lives
  // *inside* the Canvas and can't catch that failure).
  if (!webglAvailable) {
    return (
      <div className="relative h-full w-full">
        <BrainCanvasUnavailable />
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className="relative h-full w-full"
      aria-busy={!ready}
      onPointerDown={onCanvasPointerDown}
    >
      <Canvas
        // dpr capped to keep the FPS floor on 2021 MBP M1.
        dpr={[1, 2]}
        camera={{ position: [0, 0, 3.2], fov: 35 }}
        gl={{ antialias: true, alpha: true, powerPreference: "high-performance" }}
        // Force `always` while the tour runs so the rAF-driven camera
        // updates from useTour actually render every frame even when
        // prefers-reduced-motion would otherwise switch us to demand mode.
        frameloop={reduceMotion && !tour.active ? "demand" : "always"}
      >
        <color attach="background" args={["#0a0b0d"]} />
        <ambientLight intensity={0.45} />
        <directionalLight position={[2.5, 3, 2]} intensity={0.9} />
        <directionalLight position={[-2, -1, -1.5]} intensity={0.25} />

        <SurfaceErrorBoundary
          fallback={
            <PlaceholderMesh
              byRegion={frame.byRegion}
              range={frame.range}
              onReady={handleReady}
            />
          }
        >
          <Suspense
            // Suspense fallback uses the wireframe placeholder so loading
            // reads as "we know the brain is on its way" rather than
            // "here's a stand-in shape, hope you don't notice the swap."
            // The error boundary above still renders the solid placeholder
            // — those two states are visually distinguishable.
            fallback={
              <PlaceholderMesh
                byRegion={frame.byRegion}
                range={frame.range}
                onReady={handleReady}
                wireframe
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
                range={frame.range}
                onReady={handleReady}
              />
            ) : surfaceAvailable ? (
              <CorticalSurface
                url={SURFACE_URL}
                byRegion={frame.byRegion}
                perVertex={frame.perVertex ?? undefined}
                range={frame.range}
                vertexRegions={regionMask ?? undefined}
                regionOrder={REGION_IDS}
                highlightRegion={tour.active ? tour.frame.highlightedRegion : null}
                highlightDamped={reduceMotion}
                // Camera presets target an absolute pose; the surface
                // group's idle auto-rotation would drag the orientation
                // out from under them immediately after a snap. Freeze
                // when the user has parked a preset OR a tour is active.
                freezeRotation={
                  (tour.active && !reduceMotion) || cameraPreset !== null
                }
                playheadSec={effectivePlayheadSec}
                onReady={handleReady}
                onHover={handleHover}
              />
            ) : (
              <PlaceholderMesh
                byRegion={frame.byRegion}
                range={frame.range}
                onReady={handleReady}
              />
            )}
          </Suspense>
        </SurfaceErrorBoundary>

        <OrbitControls
          ref={orbitControlsRef}
          enablePan={false}
          enableZoom={false}
          enableDamping={!reduceMotion}
          dampingFactor={0.08}
          rotateSpeed={0.6}
        />

        <TourCameraDriver
          active={tour.active}
          frame={tour.frame}
          controlsRef={orbitControlsRef}
          skipCameraDrive={reduceMotion}
        />

        <CameraPresetDriver
          preset={cameraPreset}
          requestId={cameraRequestId}
          controlsRef={orbitControlsRef}
          // Defer to the tour while it's running; the two drivers
          // would otherwise stomp on each other's per-frame writes.
          suspended={tour.active}
          // Reduced motion: snap instantly, no easing. The user opted
          // out of animation; a 300ms tween is exactly the kind of
          // thing they're filtering for.
          snap={reduceMotion}
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
        <RegionLegend byRegion={frame.byRegion} range={frame.range} />
      )}

      {/* Tour button — same chrome aesthetic as the other overlays. Gated
          on the same `hasKeyframes` predicate the legend uses so it never
          appears in Dashboard hero mode where the scrubber + region
          spotlight have nothing to drive. Sits to the LEFT of the legend
          (RegionLegend is right-3 with w-[200px], so its left edge is
          ~203px from the right; we anchor at right-[212px] for a small
          gap). data-tour-button lets the canvas-level pointer-down
          cancellation skip clicks on this button. */}
      {hasKeyframes && (
        <button
          type="button"
          data-tour-button
          onClick={(e) => {
            // Stop bubbling so the wrapper's onPointerDown cancel-on-click
            // doesn't immediately re-cancel a just-started tour. (Click
            // fires after pointerdown but stopping propagation is still
            // worth it as a defence-in-depth.)
            e.stopPropagation();
            if (tour.active) tour.cancel();
            else tour.start();
          }}
          onPointerDown={(e) => {
            // Prevent the wrapper-level cancel from interpreting the
            // button press as a "click on the canvas" cancellation.
            e.stopPropagation();
          }}
          className={[
            "pointer-events-auto absolute right-[212px] top-3 z-10",
            "border border-line bg-surface/95 px-2.5 py-1 text-[11px]",
            "font-mono uppercase tracking-[0.08em] text-ink-200 backdrop-blur-sm",
            "transition-colors hover:border-accent hover:text-accent",
            "focus-visible:border-accent focus-visible:text-accent",
            "focus-visible:outline-none",
            tour.active ? "border-accent text-accent" : "",
          ].join(" ")}
          aria-pressed={tour.active}
          aria-label={tour.active ? "Stop tour" : "Start tour"}
        >
          {tour.active ? "Stop" : "Tour ▸"}
        </button>
      )}

      {/* Tour status caption — overlays the bottom of the viewport while
          the tour runs, labelling the currently-spotlit region. Plain
          DOM so it doesn't compete with the cortical surface for GPU
          time. */}
      {tour.active && tour.frame.highlightedRegion && (
        <div
          role="status"
          aria-live="polite"
          className={[
            "pointer-events-none absolute left-1/2 top-3 z-10 -translate-x-1/2",
            "border border-line bg-surface/95 px-3 py-1.5 backdrop-blur-sm",
            "text-center text-[11px] leading-tight text-ink-100 shadow-sm",
          ].join(" ")}
        >
          <div className="font-mono uppercase tracking-[0.08em] text-accent">
            {tour.frame.highlightedRegion}
          </div>
          <div className="mt-0.5 text-[10.5px] text-ink-300">
            {REGION_DESCRIPTIONS[tour.frame.highlightedRegion]}
          </div>
        </div>
      )}

      {/* Camera preset bar. Bottom-left of the viewport, out of the way
          of the legend (top-right) and the activation-purged banner
          (bottom-center). Hidden on the Dashboard hero where the
          presets would just spam buttons against the small mesh. */}
      {hasKeyframes && (
        <div
          role="toolbar"
          aria-label="Camera presets"
          className={[
            "pointer-events-auto absolute bottom-3 left-3 z-10",
            // Hidden on phones — six small targets crowd a narrow canvas and
            // are fiddly to tap; drag-to-rotate still works there.
            "hidden items-stretch gap-px overflow-hidden border border-line sm:flex",
            "bg-surface/95 font-mono text-[10px] uppercase tracking-[0.06em]",
            "text-ink-200 backdrop-blur-sm shadow-sm",
          ].join(" ")}
        >
          {CAMERA_PRESET_BUTTONS.map((b) => (
            <button
              key={b.preset}
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                requestPreset(b.preset);
              }}
              onPointerDown={(e) => e.stopPropagation()}
              title={b.title}
              aria-label={b.title}
              aria-pressed={cameraPreset === b.preset}
              className={[
                "px-2 py-1 transition-colors hover:bg-ink-900/40",
                "hover:text-accent focus-visible:text-accent",
                "focus-visible:outline-none focus-visible:bg-ink-900/40",
                cameraPreset === b.preset ? "text-accent" : "",
              ].join(" ")}
            >
              {b.label}
            </button>
          ))}
        </div>
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

      {/* Loading skeleton — covers the canvas box until the first frame
          paints, so there's no flash of an empty dark canvas while WebGL
          initialises and the GLB parses. Kept mounted and faded out (not
          unmounted) so the reveal is a smooth cross-fade. pointer-events-none
          + aria-hidden so it never intercepts input or double-announces. */}
      <div
        aria-hidden
        className={[
          "pointer-events-none absolute inset-0 z-20 transition-opacity duration-500",
          ready ? "opacity-0" : "opacity-100",
        ].join(" ")}
      >
        <BrainCanvasSkeleton />
      </div>
      {!ready && (
        <span role="status" className="sr-only">
          Loading brain visualisation…
        </span>
      )}
    </div>
  );
}

export default BrainMesh;
