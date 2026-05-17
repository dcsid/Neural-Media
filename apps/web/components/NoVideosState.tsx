import {
  EmptyState,
  EmptyStatePrimaryCta,
  EmptyStateStep,
} from "./EmptyState";

interface NoVideosStateProps {
  // The view the user landed on. Used to tailor the headline only.
  scope?: "dashboard" | "compare";
}

// Rendered when the API is reachable but there are no videos yet — either
// the user hasn't imported their TikTok export, or hasn't run `make
// sample` on a fresh install. /import is now the primary action; the CLI
// stays for power users who'd rather skip the drop zone.
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
          <EmptyStatePrimaryCta
            index={1}
            label="Import your TikTok export"
            heading="Drop your export at /import"
            href="/import"
            hint={
              <>
                Drag-and-drop your{" "}
                <code className="font-mono text-ink-200">user_data.json</code>{" "}
                or the original{" "}
                <code className="font-mono text-ink-200">.zip</code> archive.
                Nothing leaves your machine.
              </>
            }
          />
          <EmptyStateStep
            index={2}
            label="Or, from the command line"
            command="python -m neural_media_pipeline.importer data/raw/user_data.json"
            hint={
              <>
                Equivalent path for users who would rather not touch a
                browser. The same pipeline runs either way.
              </>
            }
          />
        </>
      }
      diagnostic={
        <p>
          Just here for a look around? Run{" "}
          <code className="font-mono text-ink-300">make sample</code> to
          generate a deterministic set of mock TRIBE outputs and explore the
          views without ingesting your own export.
        </p>
      }
    >
      Neural Media analyses videos that exist in your local watch history.
      The API is reachable, but the catalogue is empty. Drop your TikTok
      export to see your{" "}
      <span className="text-ink-50">
        predicted average cortical response
      </span>
      .
    </EmptyState>
  );
}
