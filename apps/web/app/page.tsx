import { Suspense } from "react";
import { api, ApiError, serverBaseUrl } from "@/lib/api";
import {
  isAllMockRuns,
  meanActivationAcrossRegions,
} from "@/lib/dashboard-metrics";
import {
  formatDateRange,
  formatWatchTimeHuman,
} from "@/lib/format";
import { ApiOfflineState } from "@/components/ApiOfflineState";
import { NoVideosState } from "@/components/NoVideosState";
import { ImportInProgressState } from "@/components/ImportInProgressState";
import { AutoPlayingBrain } from "@/components/AutoPlayingBrain";
import { BrainMeshSlot } from "@/components/BrainMeshSlot";
import { ActivationScale } from "@/components/brain";
import { DayStrip } from "@/components/DayStrip";
import { WatchedVideosSection } from "@/components/WatchedVideosSection";
import { WatchedVideosListSkeleton } from "@/components/WatchedVideosListSkeleton";
import { StatRow } from "@/components/StatRow";
import { HeroFinding } from "@/components/HeroFinding";
import { RegionLeaderboard } from "@/components/RegionLeaderboard";
import { HourHistogramAnnotated } from "@/components/HourHistogramAnnotated";
import { AuthorPlaceholder } from "@/components/AuthorPlaceholder";
import { DemoModeBanner } from "@/components/DemoModeBanner";
import { REGION_DESCRIPTIONS, type RegionDef } from "@shared/types";

export const dynamic = "force-dynamic";

interface DashboardPageProps {
  searchParams: Promise<{ demo?: string }>;
}

export default async function DashboardPage({ searchParams }: DashboardPageProps) {
  const baseUrl = serverBaseUrl();
  const params = await searchParams;
  const demo = params.demo === "1" || params.demo === "true";
  const opts = { baseUrl, demo };
  try {
    const aggregate = await api.aggregate(opts);

    if (aggregate.total_videos === 0) {
      return <NoVideosState scope="dashboard" />;
    }

    // Mid-import refresh: the catalogue already has videos but no
    // inference run has landed yet, so every region bucket reports a
    // zero mean and zero peak. Rendering the populated dashboard in
    // that state shows flat region bars and a "0.00" mean activation,
    // which reads as "TikTok activates nothing" rather than "no data
    // yet". Render a dedicated waiting state instead.
    const hasAnyActivation = Object.values(aggregate.by_region).some(
      (b) => (b?.peak ?? 0) > 0 || (b?.mean ?? 0) > 0,
    );
    if (!hasAnyActivation) {
      const inferenceRuns = await api
        .inferenceRuns(opts)
        .catch(() => []);
      const processed = new Set(
        inferenceRuns
          .filter((r) => r.status === "complete")
          .map((r) => r.video_id),
      ).size;
      return (
        <ImportInProgressState
          videosParsed={aggregate.total_videos}
          videosProcessed={processed}
        />
      );
    }

    // Region descriptions for the leaderboard hover. Server-side fetch
    // so we render with the API's canonical text; if the call fails
    // we fall back to the contract's REGION_DESCRIPTIONS constant.
    const regionDefs: RegionDef[] = await api
      .regions(opts)
      .catch(() => []);
    const descriptions: Record<string, string> = { ...REGION_DESCRIPTIONS };
    for (const def of regionDefs) {
      descriptions[def.region_id] = def.description;
    }

    const meanActivation = meanActivationAcrossRegions(aggregate.by_region);

    // Fetch one representative video's activation so the dashboard can
    // show the animated brain (per-frame interpolation) alongside the
    // static aggregate. "Representative" = the first video in catalogue
    // order whose activation actually loads; if every fetch fails we
    // just skip the animated mesh and render the static one alone.
    const videosForAnim = await api.videos(opts).catch(() => []);
    let animActivation: Awaited<ReturnType<typeof api.videoActivation>> | null = null;
    for (const v of videosForAnim.slice(0, 5)) {
      try {
        animActivation = await api.videoActivation(v.id, opts);
        break;
      } catch {
        // Try the next one — purged activations, etc.
      }
    }

    // Mock-mode detection: the displayed predictions don't reflect what
    // was IN any video — they're deterministic functions of the URL
    // hash via MockBackend. In that case, surfacing the source URL adds
    // noise (the URL doesn't explain the numbers next to it) and, on
    // the demo dataset specifically, leaks someone else's history. Hide
    // per-row URLs whenever every recent inference run is mock; the
    // aggregates above still tell the story.
    const runsForMockCheck = await api.inferenceRuns(opts).catch(() => []);
    const allMock = isAllMockRuns(runsForMockCheck);

    return (
      <main className="mx-auto max-w-[1280px] px-8 pb-10 pt-12">
        {demo ? <DemoModeBanner /> : null}

        <section className="motion-fade-in">
          <p className="eyebrow">Brain mesh</p>
          <h2 className="mt-2 font-serif text-[20px] tracking-tightish text-ink-50">
            Predicted average BOLD on the fsaverage5 surface
          </h2>
          <div className="mt-6 grid gap-6 md:grid-cols-[1fr_1fr_auto] md:items-stretch">
            <div>
              <p className="eyebrow mb-2 text-ink-400">Over time · single video (auto-playing)</p>
              {animActivation ? (
                <AutoPlayingBrain
                  activation={animActivation}
                  meanActivation={meanActivation}
                />
              ) : (
                <BrainMeshSlot activation={meanActivation} />
              )}
            </div>
            <div>
              <p className="eyebrow mb-2 text-ink-400">Static · averaged over the window</p>
              <BrainMeshSlot activation={meanActivation} />
            </div>
            <ActivationScale orientation="vertical" />
          </div>
          <p className="mt-4 max-w-[70ch] text-[12px] leading-relaxed text-ink-300">
            Left: predicted activation across one representative video,
            interpolated per timepoint — drag the scrubber to step through.
            Right: averaged across the entire window. Same colormap on both
            so the comparison reads directly. Brighter regions are higher
            predicted activation.{" "}
            <span className="text-ink-400">
              Predicted average — not your individual brain.
            </span>
          </p>
        </section>

        <HeroFinding
          totalVideos={aggregate.total_videos}
          totalWatchTimeS={aggregate.total_watch_time_s}
          byRegion={aggregate.by_region}
          firstWatchedAt={aggregate.first_watched_at}
          lastWatchedAt={aggregate.last_watched_at}
        />

        <div className="mt-10">
          <StatRow
            items={[
              {
                label: "Videos",
                value: aggregate.total_videos.toLocaleString(),
              },
              {
                label: "Watch time",
                value: formatWatchTimeHuman(aggregate.total_watch_time_s),
              },
              {
                label: "Mean activation",
                value: meanActivation.toFixed(2),
                hint: "averaged across regions",
              },
              {
                label: "Date range",
                value: formatDateRange(
                  aggregate.first_watched_at,
                  aggregate.last_watched_at,
                ),
              },
            ]}
          />
        </div>

        <RegionLeaderboard
          byRegion={aggregate.by_region}
          descriptions={descriptions}
        />

        <HourHistogramAnnotated
          byHourOfDay={aggregate.by_hour_of_day}
          byRegion={aggregate.by_region}
        />

        <DayStrip byDayOfWeek={aggregate.by_day_of_week} />

        <AuthorPlaceholder byAuthor={aggregate.by_author} />

        {allMock ? (
          // Mock-mode hides the raw watch-history URLs everywhere
          // (both the demo dataset and the user's own mock imports).
          // Two reasons that compose: (1) the displayed predictions
          // are SHA-256(URL)→sine-wave outputs, so the URL doesn't
          // explain the colours next to it — surfacing it is noise;
          // (2) on the baked demo dataset specifically, the URLs are
          // someone else's history and shouldn't be redistributed.
          // Real-mode runs (when they land) will show the URLs because
          // the model actually reads each one's content.
          <section className="motion-fade-in border-t border-line py-10">
            <p className="eyebrow">Watch history</p>
            <p className="mt-2 text-[12px] leading-relaxed text-ink-300">
              Per-video URLs are hidden while predictions come from the
              mock backend — the URL is the only thing the mock backend
              reads (it hashes to a seed), so showing it next to the
              numbers would be misleading. Aggregate metrics above
              cover the full set.{" "}
              {demo ? null : (
                <span className="text-ink-400">
                  Real-mode runs (which actually read the video frames
                  + audio) will surface the URL alongside each row.
                </span>
              )}
            </p>
          </section>
        ) : (
          <Suspense fallback={<WatchedVideosListSkeleton />}>
            <WatchedVideosSection demo={demo} />
          </Suspense>
        )}
      </main>
    );
  } catch (err) {
    if (err instanceof ApiError) {
      return <ApiOfflineState url={err.url} message={err.message} />;
    }
    throw err;
  }
}
