import clsx from "clsx";
import { type AggregateBucket, type RegionId } from "@shared/types";
import { formatHour, regionShortLabel } from "@/lib/format";

interface HourHistogramAnnotatedProps {
  byHourOfDay: number[];
  // Used to name the dominant region for the peak-hour callout. We don't
  // have per-hour-per-region data on AggregateReport yet, so the callout
  // attributes the peak hour to the top region overall — honest until
  // by_author / per-hour pivots land.
  byRegion: Record<RegionId, AggregateBucket>;
}

// 24-bar hour histogram with a callout annotating the peak hour. Reuses
// the visual idiom from HourHistogram.tsx (3px gaps, 80px bar height,
// accent/70 fills) so the chart reads as part of the same chart family.
export function HourHistogramAnnotated({
  byHourOfDay,
  byRegion,
}: HourHistogramAnnotatedProps) {
  const bars = Array.from({ length: 24 }, (_, i) => byHourOfDay[i] ?? 0);
  const peak = Math.max(1e-6, ...bars);
  const peakHour = bars.reduce(
    (best, value, i) => (value > bars[best] ? i : best),
    0,
  );
  const peakValue = bars[peakHour];
  const topRegion = pickTopRegionId(byRegion);

  // The callout's horizontal position. Each column is at (hour + 0.5) /
  // 24 of the grid width. CSS uses calc so the caret stays anchored
  // even when the grid reflows.
  const calloutLeft = `calc(${((peakHour + 0.5) / 24) * 100}% - 6px)`;

  return (
    <section className="motion-fade-in border-t border-line py-10">
      <p className="eyebrow">When you watch</p>
      <h2 className="mt-2 font-serif text-[20px] tracking-tightish text-ink-50">
        Predicted activation by hour
      </h2>

      <div className="relative mt-8">
        <div className="grid items-end gap-[3px]" style={gridStyle}>
          {bars.map((value, hour) => {
            const heightPct = (value / peak) * 100;
            const isPeak = hour === peakHour && peakValue > 0;
            return (
              <div
                key={hour}
                className="flex flex-col items-center"
                title={`${formatHour(hour)} — predicted activation ${value.toFixed(2)}`}
              >
                <div className="flex h-[80px] w-full items-end">
                  <div
                    className={clsx(
                      "w-full transition-colors",
                      value > 0
                        ? isPeak
                          ? "bg-accent"
                          : "bg-accent/70"
                        : "bg-line",
                    )}
                    style={{ height: `${Math.max(2, heightPct)}%` }}
                  />
                </div>
              </div>
            );
          })}
        </div>
        <div
          className="mt-2 grid text-[10px] text-ink-400"
          style={gridStyle}
          aria-hidden
        >
          {bars.map((_, hour) => (
            <div
              key={hour}
              className={clsx(
                "text-center",
                hour === peakHour ? "text-accent" : "",
              )}
            >
              {hour % 6 === 0 || hour === peakHour ? formatHour(hour) : ""}
            </div>
          ))}
        </div>

        {peakValue > 0 && (
          <div
            className="pointer-events-none absolute top-[-18px] flex flex-col items-center"
            style={{ left: calloutLeft }}
          >
            <span className="font-mono text-[10px] uppercase tracking-wider text-accent">
              Peak
            </span>
            <span className="h-2 w-[1px] bg-accent" aria-hidden />
          </div>
        )}
      </div>

      {peakValue > 0 && topRegion ? (
        <p className="mt-6 max-w-[60ch] text-[13px] leading-relaxed text-ink-200">
          <span className="text-ink-50">Peak: {formatHour(peakHour)}</span> —
          you tend to watch{" "}
          <span className="text-ink-100">{regionShortLabel(topRegion)}</span>
          -activating videos {hourPhrase(peakHour)}.
        </p>
      ) : (
        <p className="mt-6 max-w-[60ch] text-[13px] leading-relaxed text-ink-300">
          Not enough watch events to call a peak hour yet.
        </p>
      )}
    </section>
  );
}

function pickTopRegionId(
  byRegion: Record<RegionId, AggregateBucket>,
): RegionId | null {
  let best: { id: RegionId; mean: number } | null = null;
  for (const id of Object.keys(byRegion) as RegionId[]) {
    const bucket = byRegion[id];
    if (!bucket) continue;
    if (best == null || bucket.mean > best.mean) {
      best = { id, mean: bucket.mean };
    }
  }
  if (!best || best.mean <= 0) return null;
  return best.id;
}

// Soft natural-language descriptor of when a given hour lives. The peak
// hour is named explicitly in the sentence; this just colours the time
// of day so the copy doesn't sound robotic.
function hourPhrase(hour: number): string {
  if (hour >= 22 || hour < 4) return "late at night";
  if (hour >= 18) return "in the evening";
  if (hour >= 12) return "in the afternoon";
  if (hour >= 5) return "in the morning";
  return "in the small hours";
}

const gridStyle = { gridTemplateColumns: "repeat(24, minmax(0, 1fr))" } as const;
