import { api, serverBaseUrl } from "@/lib/api";

// Site-wide mock-mode notice. Server component that fetches the live
// /api/v1/inference-runs list once per render; only mounts when at least
// one complete run was produced by a mock backend (model_id starts with
// "tribe-v2-mock"). When every complete run is real ("tribe-v2") — or the
// catalogue is empty, or the API is offline — the banner stays hidden.
//
// Read the badge copy literally as "this is synthetic output, treat the
// numbers below as a demo, not a reading of your brain." See
// docs/scientific-framing.md (mirrored in-app at /about).

// This badge lives in the root layout, so its fetch runs during EVERY
// route's server-render. Bound it with a short timeout: if the API is slow
// or unreachable (e.g. during e2e, or before the local API has started) the
// fetch must never stall the page's SSR — on timeout we simply hide the
// badge. Without this, an offline API blocks the server-render of every
// route (the fetch hangs rather than failing fast, so the catch never runs).
const BADGE_API_TIMEOUT_MS = 2_000;

export async function MockModeBadge() {
  let hasMock = false;
  try {
    const runs = await api.inferenceRuns({
      baseUrl: serverBaseUrl(),
      signal: AbortSignal.timeout(BADGE_API_TIMEOUT_MS),
    });
    hasMock = runs.some(
      (r) => r.status === "complete" && r.model_id.startsWith("tribe-v2-mock"),
    );
  } catch {
    // API unreachable, slow, or timed out — render nothing. The badge has
    // no useful signal to add, and must never stall SSR.
    return null;
  }
  if (!hasMock) return null;

  return (
    <div
      role="note"
      aria-label="Mock-mode notice"
      className="sticky top-0 z-30 border-b border-line bg-surface/95 backdrop-blur-[2px]"
    >
      <div className="mx-auto flex max-w-[1280px] flex-wrap items-center justify-between gap-x-6 gap-y-1 px-8 py-2 text-[12px] leading-tight">
        <p className="text-ink-200">
          <span
            aria-hidden
            className="mr-2 inline-block h-1.5 w-1.5 -translate-y-px rounded-full bg-accent align-middle"
          />
          <span className="text-ink-100">Mock predictions</span>
          {" — "}
          synthetic outputs, no video was read.
        </p>
        <a
          href="/about"
          className="text-ink-300 underline-offset-2 hover:text-accent focus:text-accent"
        >
          See scientific framing →
        </a>
      </div>
    </div>
  );
}
