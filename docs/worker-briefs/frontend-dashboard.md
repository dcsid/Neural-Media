# Worker brief — frontend-dashboard

## Mission

Build the three core views — Dashboard, Video Detail, Compare — to a
"scientific instrument meets premium media analytics" bar. The brain mesh
is the hero; you own everything else that frames it. The brain-viz worker
plugs the 3D component into a slot you provide.

## Owned files / directories

- `apps/web/app/**` — except `apps/web/app/api/**` (not used; we proxy
  to FastAPI via `next.config.ts` rewrites).
- `apps/web/components/**` — except `apps/web/components/brain/**`,
  which is owned by brain-viz.
- `apps/web/lib/**` — typed API client (`lib/api.ts`), formatters,
  client utilities.
- `apps/web/public/**` — static assets (fonts, favicons).
- `apps/web/tailwind.config.ts` — design tokens (you may extend, but
  resist adding accent colors beyond the one in scaffold).
- `apps/web/app/globals.css`.
- `apps/web/README.md` if you want to add one.

## Files this worker must NOT touch

- `shared/**` — types live here. Add a re-export under
  `apps/web/lib/types.ts` if you want, but mirror, don't redefine.
- `apps/web/components/brain/**` — owned by brain-viz.
- `apps/web/next.config.ts` (security headers + rewrites). Coordinate
  with the integration lead if you need to change them.
- `apps/web/package.json` deps. Adding a heavy dependency is a
  coordinated change — prefer Motion for React and the existing R3F
  setup.

## Deliverables

1. **Dashboard view (`app/page.tsx`)**
   - Hero: brain mesh slot + a one-paragraph honest framing line.
   - Region balance: one horizontal track per region (8 regions from
     `shared/types.ts:REGION_IDS`), mean + peak readings.
   - Engagement-by-hour histogram (24 bars) and by-day-of-week strip.
   - Watched-videos list linking to `/v/{id}`.
2. **Video Detail view (`app/v/[id]/page.tsx`)**
   - Brain mesh slot synced to a timeline scrubber.
   - Per-region readings (mean / peak / sustained).
   - Per-region sparkline timeseries.
   - Source-URL + duration.
3. **Compare view (`app/compare/page.tsx`)**
   - Two-up picker — pick any two videos or time periods.
   - Side-by-side per-region timelines with shared y-axis.
   - One difference reading per region.
4. **Typed API client**: `apps/web/lib/api.ts` wrapping every endpoint
   in `shared/types.ts:ENDPOINTS`. Errors render an "API offline" state
   that tells the user how to start the backend.
5. **Polish pass**: tabular numerals everywhere, hairlines instead of
   card borders, generous whitespace, no `rounded-2xl` look.

## Interfaces this worker must preserve

- The set of API endpoints in `shared/types.ts`. If you need a new one,
  open it in `shared/CONTRACTS.md` first, then ping api-orchestrator.
- The brain-mesh slot is a component imported from
  `apps/web/components/brain/` — propose a name with brain-viz before
  using it elsewhere. Suggested first pass:

  ```ts
  // apps/web/components/brain/BrainMesh.tsx (owned by brain-viz)
  interface BrainMeshProps {
    activation: number;                    // dashboard hero, 0..1
    keyframeVertices?: Record<string, number[]>;  // detail view
    onScrub?: (t: number) => void;
  }
  ```

  Until brain-viz lands the real component, render the placeholder
  exposed by them.

## How to test the work

```
cd apps/web
pnpm install   # or npm install
pnpm typecheck
pnpm dev
# open http://localhost:3000 with the API running on :8000
```

Required browser pass:

- Dashboard renders against the FastAPI vertical-slice output.
- Detail view loads `/v/{id}` for every id in the sample.
- API-offline state renders cleanly with the FastAPI server stopped.
- Lighthouse / a11y: keyboard navigable, no axe violations on the
  three core views.

## Scientific-framing constraints

(See `docs/scientific-framing.md` for the canonical list — these are the
ones that matter most on the frontend.)

- The hero copy on the Dashboard must include "predicted average
  cortical response" or "predicted average BOLD response" verbatim.
- Tooltips and chart titles use "predicted activation," never "brain
  activity" / "engagement" without qualification.
- No "Wrapped-style" hype copy. No emoji. No motivational language.
- Single accent color (amber). Resist adding a second.

## Out of scope for this worker

- The 3D mesh itself (brain-viz).
- API implementation.
- Pipeline / model code.
- Auth, analytics, marketing site, SEO.
