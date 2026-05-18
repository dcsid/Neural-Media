import { Suspense } from "react";
import { api, ApiError, serverBaseUrl } from "@/lib/api";
import {
  formatDateRange,
  formatWatchTimeHuman,
} from "@/lib/format";
import { ApiOfflineState } from "@/components/ApiOfflineState";
import { NoVideosState } from "@/components/NoVideosState";
import { ImportInProgressState } from "@/components/ImportInProgressState";
import { BrainMeshSlot } from "@/components/BrainMeshSlot";
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

    const meanActivation =
      Object.values(aggregate.by_region).reduce(
        (acc, b) => acc + (b?.mean ?? 0),
        0,
      ) / Math.max(1, Object.keys(aggregate.by_region).length);

    return (
      <main className="mx-auto max-w-[1280px] px-8 pb-10 pt-12">
        {demo ? <DemoModeBanner /> : null}
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

        <section className="motion-fade-in border-t border-line py-10">
          <p className="eyebrow">Brain mesh</p>
          <h2 className="mt-2 font-serif text-[20px] tracking-tightish text-ink-50">
            Predicted average BOLD on the fsaverage5 surface
          </h2>
          <div className="mt-6 grid gap-8 md:grid-cols-[1.4fr_1fr] md:items-center">
            <BrainMeshSlot activation={meanActivation} />
            <p className="text-[12px] leading-relaxed text-ink-300">
              The cortical mesh shows the predicted average BOLD response
              across 20,484 vertices on the fsaverage5 surface, summarised
              over your watch history.{" "}
              <span className="text-ink-400">
                Predicted average — not your individual brain.
              </span>
            </p>
          </div>
        </section>

        <Suspense fallback={<WatchedVideosListSkeleton />}>
          <WatchedVideosSection demo={demo} />
        </Suspense>
      </main>
    );
  } catch (err) {
    if (err instanceof ApiError) {
      return <ApiOfflineState url={err.url} message={err.message} />;
    }
    throw err;
  }
}
