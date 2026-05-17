import Link from "next/link";
import { EmptyState, EmptyStateStep } from "./EmptyState";

interface NoVideosStateProps {
  // The view the user landed on. Used to tailor the headline only.
  scope?: "dashboard" | "compare";
}

// Rendered when the API is reachable but there are no videos yet — either
// the user hasn't run `make sample` on a fresh install, or hasn't imported
// their TikTok export. The CTA covers both paths.
export function NoVideosState({ scope = "dashboard" }: NoVideosStateProps) {
  const headline =
    scope === "compare"
      ? "Nothing to compare yet."
      : "No videos in this snapshot yet.";

  return (
    <EmptyState
      eyebrow={scope === "compare" ? "Compare" : "Dashboard"}
      headline={headline}
      cta={
        <>
          <EmptyStateStep
            index={1}
            label="Use the mock sample"
            command="make sample"
            hint={
              <>
                Builds a deterministic set of mock TRIBE outputs so you can
                explore the views without ingesting your own export.
              </>
            }
          />
          <EmptyStateStep
            index={2}
            label="Import your TikTok export"
            command="python -m neural_media_pipeline.importer data/raw/user_data.json"
            hint={
              <>
                Drop a TikTok &quot;Download your data&quot; archive at{" "}
                <code className="font-mono">data/raw/user_data.json</code>{" "}
                first. A guided import flow will live at{" "}
                <Link
                  href="/import"
                  className="text-ink-100 underline-offset-2 hover:text-accent hover:underline focus:text-accent focus:underline focus:outline-none"
                >
                  /import
                </Link>{" "}
                once the upload UI lands.
              </>
            }
          />
        </>
      }
    >
      Neural Media analyses videos that exist in your local watch history.
      The API is reachable, but the catalogue is empty. Generate the mock
      sample for a quick tour, or import your TikTok export to see your{" "}
      <span className="text-ink-50">
        predicted average cortical response
      </span>
      .
    </EmptyState>
  );
}
