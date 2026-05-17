import {
  REGION_IDS,
  type AggregateBucket,
  type RegionId,
} from "@shared/types";
import { formatWatchTimeHuman, regionLabel, regionShortLabel } from "@/lib/format";

interface HeroFindingProps {
  totalVideos: number;
  totalWatchTimeS: number;
  byRegion: Record<RegionId, AggregateBucket>;
  firstWatchedAt: string | null;
  lastWatchedAt: string | null;
}

// The Wrapped opening sentence. One assertion, one number, one region. Copy
// follows scientific-framing.md: "predicted average cortical response" and
// "predicted to engage", never "your brain" or "engages".
export function HeroFinding({
  totalVideos,
  totalWatchTimeS,
  byRegion,
  firstWatchedAt,
  lastWatchedAt,
}: HeroFindingProps) {
  const top = pickTopRegion(byRegion);
  const windowDescriptor = formatWindow(firstWatchedAt, lastWatchedAt);

  return (
    <section className="motion-fade-in">
      <p className="eyebrow mb-4">The headline finding</p>
      <h1 className="font-serif text-[40px] leading-[1.1] tracking-tightish text-ink-50">
        Across {windowDescriptor}, you watched{" "}
        <span className="text-accent" data-num>
          {totalVideos.toLocaleString()}
        </span>{" "}
        videos —{" "}
        <span className="text-ink-100" data-num>
          {formatWatchTimeHuman(totalWatchTimeS)}
        </span>{" "}
        in total.
        {top ? (
          <>
            {" "}Your most-activated region was{" "}
            <span className="text-accent">{regionShortLabel(top.id)}</span>
            <span className="text-ink-300"> ({regionLabel(top.id).toLowerCase()})</span>
            , predicted to engage at{" "}
            <span className="text-ink-100" data-num>
              {top.bucket.mean.toFixed(2)}
            </span>{" "}
            on average.
          </>
        ) : null}
      </h1>
      <p className="mt-5 max-w-[60ch] text-[14px] leading-relaxed text-ink-200">
        Each video in your watch history was passed through Meta FAIR TRIBE v2
        to estimate the{" "}
        <span className="text-ink-50">predicted average BOLD response</span>{" "}
        across the 720 subjects TRIBE was trained on. Numbers below describe
        that prediction, not your individual brain.
      </p>
    </section>
  );
}

interface TopRegion {
  id: RegionId;
  bucket: AggregateBucket;
}

function pickTopRegion(
  byRegion: Record<RegionId, AggregateBucket>,
): TopRegion | null {
  let best: TopRegion | null = null;
  for (const id of REGION_IDS) {
    const bucket = byRegion[id];
    if (!bucket) continue;
    if (best == null || bucket.mean > best.bucket.mean) {
      best = { id, bucket };
    }
  }
  if (!best || best.bucket.mean <= 0) return null;
  return best;
}

// Human-readable window string from the first/last watched timestamps.
// Three buckets so the hero sentence reads naturally:
//   < 36h     → "the last N hours"
//   < 14 days → "the last N days"
//   else      → "{Month D} – {Month D[, YYYY]}" (matches formatDateRange)
function formatWindow(first: string | null, last: string | null): string {
  if (!first || !last) return "your watch history";
  const a = new Date(first);
  const b = new Date(last);
  if (Number.isNaN(a.getTime()) || Number.isNaN(b.getTime())) {
    return "your watch history";
  }
  const ms = b.getTime() - a.getTime();
  const hours = ms / (1000 * 60 * 60);
  if (hours < 36) {
    const h = Math.max(1, Math.round(hours));
    return `the last ${h} ${h === 1 ? "hour" : "hours"}`;
  }
  const days = Math.round(hours / 24);
  if (days <= 14) {
    return `the last ${days} ${days === 1 ? "day" : "days"}`;
  }
  const sameYear = a.getUTCFullYear() === b.getUTCFullYear();
  const dateFmt = new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: sameYear ? undefined : "numeric",
  });
  return `${dateFmt.format(a)} – ${dateFmt.format(b)}`;
}
