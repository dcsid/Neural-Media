import clsx from "clsx";
import {
  REGION_IDS,
  type AggregateBucket,
  type RegionId,
} from "@shared/types";
import { regionShortLabel } from "@/lib/format";

interface RegionLeaderboardProps {
  byRegion: Record<RegionId, AggregateBucket>;
  // Server-supplied region descriptions from GET /api/v1/regions, keyed by
  // region_id. Used as the hover/title text for each row. Falls back to the
  // canonical REGION_DESCRIPTIONS when a region is missing from the server
  // response.
  descriptions: Record<string, string>;
}

// Ranked leaderboard of the 8 cortical regions by predicted mean activation.
// Rank #1 is highlighted; the rest fade into ink-200/300. Reuses the
// RegionBalanceBars track + tick idiom (mean fill + peak tick) so the eye
// recognises it as the same scale, just ordered.
export function RegionLeaderboard({
  byRegion,
  descriptions,
}: RegionLeaderboardProps) {
  const peakMax = Math.max(
    1e-6,
    ...REGION_IDS.map((id) => byRegion[id]?.peak ?? 0),
  );

  const ranked = REGION_IDS
    .map((id) => ({
      id,
      bucket: byRegion[id] ?? { mean: 0, peak: 0 },
    }))
    .sort((a, b) => b.bucket.mean - a.bucket.mean);

  return (
    <section className="motion-fade-in border-t border-line py-10">
      <p className="eyebrow">Top region</p>
      <h2 className="mt-2 font-serif text-[20px] tracking-tightish text-ink-50">
        The eight cortical regions, ranked
      </h2>

      <ol className="mt-8 divide-y divide-line">
        {ranked.map((row, idx) => {
          const isTop = idx === 0;
          const meanPct = clamp01(row.bucket.mean / peakMax) * 100;
          const peakPct = clamp01(row.bucket.peak / peakMax) * 100;
          const description = descriptions[row.id] ?? row.id;
          return (
            <li
              key={row.id}
              className={clsx(
                "group grid grid-cols-[28px_140px_1fr_64px_64px] items-center gap-6 py-4 text-[12px] transition-colors",
                isTop ? "bg-accent/[0.03]" : null,
              )}
              title={description}
            >
              <span
                className={clsx(
                  "font-mono tabular-nums text-[11px]",
                  isTop ? "text-accent" : "text-ink-400",
                )}
                data-num
              >
                {(idx + 1).toString().padStart(2, "0")}
              </span>
              <div>
                <p
                  className={clsx(
                    "font-serif text-[15px] tracking-tightish",
                    isTop ? "text-ink-50" : "text-ink-100",
                  )}
                >
                  {regionShortLabel(row.id)}
                </p>
                <p className="mt-0.5 text-[11px] text-ink-400 transition-colors group-hover:text-ink-200">
                  {description}
                </p>
              </div>
              <div className="relative h-[6px] bg-line">
                <div
                  className={clsx(
                    "absolute inset-y-0 left-0",
                    isTop ? "bg-accent" : "bg-accent/60",
                  )}
                  style={{ width: `${meanPct}%` }}
                />
                <div
                  className={clsx(
                    "absolute top-[-3px] h-[12px] w-[2px]",
                    isTop ? "bg-accent" : "bg-accent/80",
                  )}
                  style={{ left: `calc(${peakPct}% - 1px)` }}
                  aria-hidden
                />
              </div>
              <p
                className={clsx(
                  "text-right",
                  isTop ? "text-ink-50" : "text-ink-100",
                )}
                data-num
                title="Predicted activation, mean over watch history"
              >
                {row.bucket.mean.toFixed(2)}
              </p>
              <p
                className="text-right text-ink-300"
                data-num
                title="Predicted activation, peak over watch history"
              >
                {row.bucket.peak.toFixed(2)}
              </p>
            </li>
          );
        })}
      </ol>
      <p className="mt-4 max-w-[60ch] text-[11px] leading-relaxed text-ink-400">
        Predicted activations are unitless TRIBE v2 outputs. Region rank is
        meaningful; absolute magnitudes are not calibrated.
      </p>
    </section>
  );
}

function clamp01(x: number): number {
  if (!Number.isFinite(x)) return 0;
  if (x < 0) return 0;
  if (x > 1) return 1;
  return x;
}
