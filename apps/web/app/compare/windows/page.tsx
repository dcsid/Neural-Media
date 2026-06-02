import clsx from "clsx";
import Link from "next/link";
import {
  REGION_IDS,
  type RegionId,
  type WatchEvent,
} from "@shared/types";
import { api, ApiError, serverBaseUrl } from "@/lib/api";
import { accumulateRegionMetrics, type RegionStat } from "@/lib/window-metrics";
import { ApiOfflineState } from "@/components/ApiOfflineState";
import { NoVideosState } from "@/components/NoVideosState";
import { formatWatchTimeHuman, regionShortLabel } from "@/lib/format";

export const dynamic = "force-dynamic";

// Preset windows the page exposes. `last`/`previous` semantics mean "the
// trailing N days/hours ending now" and "the trailing N days/hours that
// ended N days/hours ago" respectively. Date pickers can come later;
// presets cover the demo story.
type Preset =
  | "week"          // last 7d vs previous 7d
  | "day"           // last 24h vs previous 24h
  | "month";        // last 30d vs previous 30d

interface PageProps {
  searchParams: Promise<{ p?: string }>;
}

interface WindowRange {
  label: string;
  from: Date;
  to: Date;
}

interface WindowAggregate {
  range: WindowRange;
  totalVideos: number;
  totalWatchTimeS: number;
  byRegion: Record<RegionId, RegionStat>;
}

const SAFE_METRICS_FETCH_CAP = 500; // Don't fire 10k parallel requests on huge histories.

export default async function CompareWindowsPage({ searchParams }: PageProps) {
  const { p } = await searchParams;
  const preset: Preset = isPreset(p) ? p : "week";
  const baseUrl = serverBaseUrl();

  try {
    const [videos, watchEvents] = await Promise.all([
      api.videos({ baseUrl }),
      api.watchEvents({ baseUrl }),
    ]);

    if (videos.length === 0) {
      return <NoVideosState scope="compare" />;
    }

    const durationByVideo = new Map<string, number>();
    for (const v of videos) durationByVideo.set(v.id, v.duration_s);

    const [rangeA, rangeB] = computeRanges(preset);

    const aggA = await aggregateWindow(rangeA, watchEvents, durationByVideo, baseUrl);
    const aggB = await aggregateWindow(rangeB, watchEvents, durationByVideo, baseUrl);

    const domainMax = Math.max(
      1e-6,
      ...REGION_IDS.flatMap((id) => [
        aggA.byRegion[id]?.peak ?? 0,
        aggB.byRegion[id]?.peak ?? 0,
      ]),
    );

    return (
      <main className="mx-auto max-w-[1280px] px-8 pb-10 pt-12">
        <p className="eyebrow mb-4">Compare · windows</p>
        <h1 className="font-serif text-[32px] tracking-tightish text-ink-50">
          Two windows of your watch history, side by side.
        </h1>
        <p className="mt-3 max-w-[60ch] text-[13px] leading-relaxed text-ink-200">
          Region bars share a y-axis so the delta is honest. Predicted
          activations are unitless TRIBE v2 outputs — comparisons are
          meaningful, absolutes are not.
        </p>

        <PresetTabs current={preset} />

        <section className="mt-8 grid gap-x-8 gap-y-3 sm:grid-cols-2">
          <WindowSummary label="Window A" agg={aggA} highlight="a" />
          <WindowSummary label="Window B" agg={aggB} highlight="b" />
        </section>

        {aggA.totalVideos === 0 || aggB.totalVideos === 0 ? (
          <EmptyWindowState aggA={aggA} aggB={aggB} />
        ) : (
          <CompareTable aggA={aggA} aggB={aggB} domainMax={domainMax} />
        )}

        <p className="mt-10 text-[11px] text-ink-400">
          Looking for the video-by-video view?{" "}
          <Link
            href="/compare"
            className="text-ink-200 underline underline-offset-2 hover:text-accent"
          >
            Compare two videos →
          </Link>
        </p>
      </main>
    );
  } catch (err) {
    if (err instanceof ApiError) {
      return <ApiOfflineState url={err.url} message={err.message} />;
    }
    throw err;
  }
}

function isPreset(value: string | undefined): value is Preset {
  return value === "week" || value === "day" || value === "month";
}

function computeRanges(preset: Preset): [WindowRange, WindowRange] {
  const now = new Date();
  let span: number;
  let labelA: string;
  let labelB: string;
  switch (preset) {
    case "day":
      span = 24 * 60 * 60 * 1000;
      labelA = "Last 24 hours";
      labelB = "Previous 24 hours";
      break;
    case "month":
      span = 30 * 24 * 60 * 60 * 1000;
      labelA = "Last 30 days";
      labelB = "Previous 30 days";
      break;
    case "week":
    default:
      span = 7 * 24 * 60 * 60 * 1000;
      labelA = "Last 7 days";
      labelB = "Previous 7 days";
      break;
  }
  const aTo = now;
  const aFrom = new Date(now.getTime() - span);
  const bTo = aFrom;
  const bFrom = new Date(aFrom.getTime() - span);
  return [
    { label: labelA, from: aFrom, to: aTo },
    { label: labelB, from: bFrom, to: bTo },
  ];
}

async function aggregateWindow(
  range: WindowRange,
  watchEvents: WatchEvent[],
  durationByVideo: Map<string, number>,
  baseUrl: string,
): Promise<WindowAggregate> {
  const inRange = watchEvents.filter((ev) => {
    const t = Date.parse(ev.watched_at);
    if (Number.isNaN(t)) return false;
    return t >= range.from.getTime() && t < range.to.getTime();
  });

  const uniqueVideoIds = new Set<string>();
  let watchTimeS = 0;
  for (const ev of inRange) {
    uniqueVideoIds.add(ev.video_id);
    const dur = ev.duration_watched_s ?? durationByVideo.get(ev.video_id) ?? 0;
    watchTimeS += dur;
  }

  // Bail before issuing a metrics flood on very large windows. The cap
  // is high enough that local demo histories sail through; the safety
  // net is for the day someone points this at a 50k-video export.
  const idsToFetch = [...uniqueVideoIds].slice(0, SAFE_METRICS_FETCH_CAP);

  const metricsLists = await Promise.all(
    idsToFetch.map((id) =>
      api
        .videoMetrics(id, { baseUrl })
        .catch((err) => {
          // A 404 means the video has no completed inference yet — skip
          // it silently rather than failing the whole comparison.
          if (err instanceof ApiError && err.status === 404) return null;
          return null;
        }),
    ),
  );

  // Accumulate mean + peak per region across the window. Videos whose
  // metrics failed to load arrive as null entries and are skipped — see
  // accumulateRegionMetrics.
  const byRegion = accumulateRegionMetrics(metricsLists);

  return {
    range,
    totalVideos: uniqueVideoIds.size,
    totalWatchTimeS: watchTimeS,
    byRegion,
  };
}

function PresetTabs({ current }: { current: Preset }) {
  const tabs: { value: Preset; label: string }[] = [
    { value: "day", label: "24 hours" },
    { value: "week", label: "7 days" },
    { value: "month", label: "30 days" },
  ];
  return (
    <nav className="mt-8 flex items-center gap-1 border-b border-line text-[12px]">
      {tabs.map((t) => {
        const active = t.value === current;
        return (
          <Link
            key={t.value}
            href={`/compare/windows?p=${t.value}`}
            className={clsx(
              "px-3 py-2 -mb-px border-b-2 transition-colors",
              active
                ? "border-accent text-ink-50"
                : "border-transparent text-ink-300 hover:text-ink-100",
            )}
          >
            {t.label}
          </Link>
        );
      })}
    </nav>
  );
}

function WindowSummary({
  label,
  agg,
  highlight,
}: {
  label: string;
  agg: WindowAggregate;
  highlight: "a" | "b";
}) {
  return (
    <div className="border-t border-line pt-6">
      <p className="eyebrow">{label}</p>
      <h2 className="mt-2 font-serif text-[20px] tracking-tightish text-ink-50">
        {agg.range.label}
      </h2>
      <div className="mt-3 flex flex-wrap items-baseline gap-x-6 gap-y-1 text-[12px] text-ink-300">
        <span>
          <span className={clsx("font-mono", highlight === "a" ? "text-accent" : "text-ink-100")} data-num>
            {agg.totalVideos}
          </span>{" "}
          videos
        </span>
        <span>
          <span className="font-mono text-ink-100" data-num>
            {formatWatchTimeHuman(agg.totalWatchTimeS)}
          </span>{" "}
          watched
        </span>
      </div>
    </div>
  );
}

function EmptyWindowState({
  aggA,
  aggB,
}: {
  aggA: WindowAggregate;
  aggB: WindowAggregate;
}) {
  const emptyOne = aggA.totalVideos === 0 ? "A" : "B";
  const range = aggA.totalVideos === 0 ? aggA.range : aggB.range;
  return (
    <section className="mt-10 border-t border-line pt-10">
      <p className="font-serif text-[20px] tracking-tightish text-ink-100">
        Window {emptyOne} ({range.label}) has no watched videos.
      </p>
      <p className="mt-3 max-w-[60ch] text-[13px] leading-relaxed text-ink-300">
        Comparison needs data on both sides.{" "}
        <Link
          href="/import"
          className="text-accent underline underline-offset-2 hover:text-ink-50"
        >
          Import more of your TikTok export
        </Link>{" "}
        — or pick a wider preset above.
      </p>
    </section>
  );
}

function CompareTable({
  aggA,
  aggB,
  domainMax,
}: {
  aggA: WindowAggregate;
  aggB: WindowAggregate;
  domainMax: number;
}) {
  return (
    <section className="mt-10 border-t border-line pt-8">
      <header className="grid grid-cols-[110px_1fr_1fr_72px] items-baseline gap-6 border-b border-line pb-3 text-[11px]">
        <span className="eyebrow">Region</span>
        <span className="eyebrow">A — {aggA.range.label}</span>
        <span className="eyebrow">B — {aggB.range.label}</span>
        <span className="eyebrow text-right">Δ mean</span>
      </header>
      <ul className="divide-y divide-line">
        {REGION_IDS.map((id) => {
          const a = aggA.byRegion[id];
          const b = aggB.byRegion[id];
          const delta = a.mean - b.mean;
          return (
            <li
              key={id}
              className="grid grid-cols-[110px_1fr_1fr_72px] items-center gap-6 py-4 text-[12px]"
            >
              <div>
                <p className="text-ink-50">{regionShortLabel(id)}</p>
              </div>
              <BarCell mean={a.mean} peak={a.peak} domainMax={domainMax} />
              <BarCell mean={b.mean} peak={b.peak} domainMax={domainMax} />
              <p
                className={clsx(
                  "text-right font-serif text-[15px] tracking-tightish",
                  Math.abs(delta) < 0.005
                    ? "text-ink-300"
                    : delta > 0
                      ? "text-accent"
                      : "text-ink-100",
                )}
                data-num
                title={`A mean − B mean = ${delta.toFixed(3)}`}
              >
                {delta > 0 ? "+" : ""}
                {delta.toFixed(2)}
              </p>
            </li>
          );
        })}
      </ul>
      <p className="mt-4 max-w-[60ch] text-[11px] leading-relaxed text-ink-400">
        Δ mean is computed in window A minus window B. Positive means the
        region was predicted to engage more strongly during the more recent
        window.
      </p>
    </section>
  );
}

function BarCell({
  mean,
  peak,
  domainMax,
}: {
  mean: number;
  peak: number;
  domainMax: number;
}) {
  const meanPct = (clamp01(mean / domainMax)) * 100;
  const peakPct = (clamp01(peak / domainMax)) * 100;
  return (
    <div className="flex items-center gap-3">
      <div className="relative h-[6px] flex-1 bg-line">
        <div
          className="absolute inset-y-0 left-0 bg-accent/70"
          style={{ width: `${meanPct}%` }}
        />
        <div
          className="absolute top-[-3px] h-[12px] w-[2px] bg-accent"
          style={{ left: `calc(${peakPct}% - 1px)` }}
          aria-hidden
        />
      </div>
      <span className="w-10 text-right text-ink-100" data-num>
        {mean.toFixed(2)}
      </span>
    </div>
  );
}

function clamp01(x: number): number {
  if (!Number.isFinite(x)) return 0;
  if (x < 0) return 0;
  if (x > 1) return 1;
  return x;
}
