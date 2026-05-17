import Link from "next/link";
import { EmptyState } from "./EmptyState";

interface ImportInProgressStateProps {
  videosParsed: number;
  videosProcessed: number;
}

// Rendered when the dashboard is refreshed mid-import: the API has
// parsed videos into the catalogue (so aggregate.total_videos > 0) but
// no inference has landed yet (every region mean / peak is still zero).
// Two routes converge on this view — a mid-pipeline refresh and the
// brief window between POST /import and the first inference run.
//
// Decision: graceful "import in progress" hold state, not NoVideosState.
// NoVideosState would imply the import didn't take, which is wrong.
// Half-rendered region bars on the populated dashboard would imply zero
// activation, which is also wrong (it's "no data yet", not "0.00").
export function ImportInProgressState({
  videosParsed,
  videosProcessed,
}: ImportInProgressStateProps) {
  const counter =
    videosParsed > 0
      ? `${videosProcessed} / ${videosParsed} videos`
      : "starting";

  return (
    <EmptyState
      eyebrow="Dashboard"
      headline="Import in progress."
      diagnostic={
        <p>
          The dashboard refreshes itself when each inference run lands.
          You can also reload this page manually to pick up new results.
        </p>
      }
    >
      Neural Media has parsed your TikTok export but is still computing
      TRIBE v2 activations for each video.{" "}
      <span className="text-ink-50 font-mono tabular-nums">{counter}</span>{" "}
      processed so far. The{" "}
      <span className="text-ink-50">
        predicted average cortical response
      </span>{" "}
      will populate as activations complete — usually under a minute for a
      typical export in mock mode. Watch the{" "}
      <Link
        href="/import"
        className="text-ink-100 underline underline-offset-2 hover:text-accent focus:text-accent"
      >
        /import
      </Link>{" "}
      tab for live progress.
    </EmptyState>
  );
}
