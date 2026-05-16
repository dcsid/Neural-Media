# Worker brief — brain-viz

## Mission

Build the 3D cortical visualization that is the hero element of the
entire product. A 20,484-vertex cortical surface that renders a
predicted-activation heatmap and scrubs in sync with a video timeline.
Use React Three Fiber + Three.js. GSAP is allowed only if the timeline
scrubber justifies it.

## Owned files / directories

- `apps/web/components/brain/**`
- `apps/web/public/brain/**` — committed surface geometry / textures /
  LUT. Keep these under a few MB; large assets go in a release artifact
  instead.
- Any worker-private hooks under
  `apps/web/components/brain/hooks/**`.

## Files this worker must NOT touch

- `shared/**`.
- Everything outside `apps/web/components/brain/**` and
  `apps/web/public/brain/**`. In particular, `apps/web/app/**` and
  `apps/web/components/**` (besides `brain/`) belong to
  frontend-dashboard.
- `apps/web/package.json` — adding heavy 3D deps is a coordinated change.

## Deliverables

1. **`BrainMesh` component** (`apps/web/components/brain/BrainMesh.tsx`)
   - Loads a cortical surface mesh. Start with `fsaverage` 5-th order or
     whatever surface TRIBE itself evaluates on; confirm with
     ml-inference before pinning.
   - Maps a per-vertex activation vector to a colour LUT.
   - Two driving modes:
     - Hero (Dashboard): single static activation value (0..1)
       modulates intensity globally.
     - Detail (Video Detail): time-varying via
       `ActivationOutput.keyframe_vertices` + interpolation.
   - Returns a clean, controllable component:

     ```ts
     interface BrainMeshProps {
       activation: number;
       keyframeVertices?: Record<string, number[]>;
       timestamps?: number[];
       playheadSec?: number;
       onReady?: () => void;
     }
     ```

2. **Timeline scrubber** synced to the video element on the Detail view.
   - Must feel responsive (pre-render LUT lookups if needed).
   - Honours `prefers-reduced-motion`.

3. **Placeholder fallback** for first render before the mesh is ready
   so the dashboard never shows a blank rectangle.

4. **Performance budget**:
   - 60fps on a 2021 MacBook Pro M1.
   - First mesh paint within 2s after route navigation.
   - Memory under 250 MB.

## Interfaces this worker must preserve

- The component prop interface above. frontend-dashboard imports it
  by name.
- The on-the-wire shape of `ActivationOutput` (CONTRACTS.md §4) —
  particularly `keyframe_vertices` and `region_means`. Do not request
  raw 20,484 × T arrays from the API.

## How to test the work

```
cd apps/web
pnpm dev
```

In the browser:

- Open the Dashboard. Mesh paints, rotates / responds to mouse, no
  console errors.
- Open `/v/{id}` for any sample video. Scrub the timeline; the heatmap
  updates within one frame.
- Toggle the OS-level "Reduce motion" setting; the mesh stops
  auto-rotating and animations short-circuit.
- Throttle the GPU to mid-tier; check the FPS overlay (`?dev=1` flag is
  yours to add) stays above 45.

## Scientific-framing constraints

- LUT colour choices matter. Use a colour-blind-safe sequential map
  (e.g. cividis or viridis-derived). Avoid red↔green dichotomies.
- Never overlay subcortical structures (NAc, VTA, amygdala) — TRIBE
  does not predict them. The mesh is **cortical surface only**.
- Tooltips on hover use region names from
  `shared/types.ts:REGION_DESCRIPTIONS`, no editorializing.

## Out of scope for this worker

- Anything outside the brain mesh and its scrubber.
- API or data work.
- Aggregation logic.
- Marketing visuals.
