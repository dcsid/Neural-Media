// Pure-DOM states for the brain canvas box — no R3F, no WebGL. Shared so the
// loading + unsupported experiences look identical wherever a brain renders
// (the /gallery + / dynamic-import fallbacks and BrainMesh's own first-paint
// overlay). Keeping these dependency-free is the point: the gallery's lazy
// loader must not pull in three.js, and the unsupported fallback has to draw
// without the very context it's reporting missing.

// Stylised brain silhouette used by both states. Symmetric on purpose so it
// reads as deliberate scaffolding rather than a broken asset.
function BrainGlyph({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 120 100"
      fill="none"
      stroke="currentColor"
      strokeWidth={2.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden
    >
      <ellipse cx="60" cy="50" rx="42" ry="33" />
      <line x1="60" y1="19" x2="60" y2="81" />
      <path d="M60 33 C 48 33 44 42 50 50" />
      <path d="M60 33 C 72 33 76 42 70 50" />
      <path d="M50 50 C 44 58 50 66 60 67" />
      <path d="M70 50 C 76 58 70 66 60 67" />
      <path d="M37 44 C 33 50 35 56 41 58" />
      <path d="M83 44 C 87 50 85 56 79 58" />
    </svg>
  );
}

// Idle background wash matching the mesh's amber-on-near-black look, so the
// box never reads as empty/unstyled while we wait.
const WASH =
  "bg-[radial-gradient(circle_at_center,rgba(245,165,36,0.06),transparent_62%)]";

// Loading skeleton: a softly pulsing brain silhouette. Decorative
// (aria-hidden) — the surrounding box owns the `aria-busy` / status text.
export function BrainCanvasSkeleton() {
  return (
    <div
      aria-hidden
      className="absolute inset-0 flex items-center justify-center overflow-hidden bg-canvas"
    >
      <div className={`absolute inset-0 ${WASH}`} />
      <BrainGlyph className="h-1/2 max-h-[150px] w-1/2 max-w-[190px] animate-pulse text-ink-500" />
    </div>
  );
}

// Graceful fallback when WebGL is unavailable. Static, no canvas — an honest
// "this needs WebGL" panel rather than a crash or a blank rectangle.
export function BrainCanvasUnavailable() {
  return (
    <div
      role="img"
      aria-label="3D brain visualisation unavailable — this browser or device does not support WebGL"
      className="absolute inset-0 flex flex-col items-center justify-center gap-3 overflow-hidden bg-canvas px-6 text-center"
    >
      <div className={`absolute inset-0 ${WASH}`} />
      <BrainGlyph className="relative h-16 w-16 text-ink-500" />
      <p className="relative font-mono text-[11px] uppercase tracking-[0.08em] text-ink-300">
        3D view unavailable
      </p>
      <p className="relative max-w-[34ch] text-[12px] leading-relaxed text-ink-400">
        This brain renders with WebGL, which isn&rsquo;t available in your
        browser or device. Everything else on the page still works.
      </p>
    </div>
  );
}
