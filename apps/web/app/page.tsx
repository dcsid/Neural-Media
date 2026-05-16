import { api, ApiError, serverBaseUrl } from "@/lib/api";
import {
  formatDateRange,
  formatWatchTimeHuman,
} from "@/lib/format";
import { ApiOfflineState } from "@/components/ApiOfflineState";
import { BrainMesh } from "@/components/brain";
import { RegionBalanceBars } from "@/components/RegionBalanceBars";
import { HourHistogram } from "@/components/HourHistogram";
import { DayStrip } from "@/components/DayStrip";
import { WatchedVideosList } from "@/components/WatchedVideosList";
import { StatRow } from "@/components/StatRow";

export const dynamic = "force-dynamic";

export default async function DashboardPage() {
  const baseUrl = serverBaseUrl();
  try {
    const [aggregate, videos, watchEvents] = await Promise.all([
      api.aggregate({ baseUrl }),
      api.videos({ baseUrl }),
      api.watchEvents({ baseUrl }),
    ]);

    const meanActivation = Object.values(aggregate.by_region).reduce(
      (acc, b) => acc + (b?.mean ?? 0),
      0,
    ) / Math.max(1, Object.keys(aggregate.by_region).length);

    return (
      <main className="mx-auto max-w-[1280px] px-8 pb-10 pt-12">
        <section className="grid gap-12 md:grid-cols-[1.05fr_1fr] md:items-center">
          <div>
            <p className="eyebrow mb-4">Dashboard</p>
            <h1 className="font-serif text-[40px] leading-[1.1] tracking-tightish text-ink-50">
              Predicted average cortical response to your TikTok history.
            </h1>
            <p className="mt-5 max-w-[60ch] text-[14px] leading-relaxed text-ink-200">
              Each video in your watch history was passed through Meta FAIR
              TRIBE v2 to estimate the{" "}
              <span className="text-ink-50">
                predicted average BOLD response
              </span>{" "}
              across the 720 subjects TRIBE was trained on. Numbers below
              describe that prediction, not your individual brain.
            </p>
            <div className="mt-8">
              <StatRow
                items={[
                  {
                    label: "Videos",
                    value: aggregate.total_videos.toString(),
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
          </div>
          <div>
            <div className="relative aspect-[5/4] w-full border border-line bg-canvas">
              <BrainMesh activation={meanActivation} />
            </div>
            <p className="mt-3 text-[11px] text-ink-400">
              Predicted average BOLD response across cortical regions.
              Falls back to a low-poly placeholder until{" "}
              <code className="font-mono">/brain/fsaverage5.glb</code>{" "}
              is committed.
            </p>
          </div>
        </section>

        <RegionBalanceBars byRegion={aggregate.by_region} />

        <div className="grid gap-12 md:grid-cols-[1.4fr_1fr]">
          <HourHistogram byHourOfDay={aggregate.by_hour_of_day} />
          <DayStrip byDayOfWeek={aggregate.by_day_of_week} />
        </div>

        <WatchedVideosList videos={videos} watchEvents={watchEvents} />
      </main>
    );
  } catch (err) {
    if (err instanceof ApiError) {
      return <ApiOfflineState url={err.url} message={err.message} />;
    }
    throw err;
  }
}
