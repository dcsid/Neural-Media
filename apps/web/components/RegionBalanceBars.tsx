import clsx from "clsx";
import {
  REGION_DESCRIPTIONS,
  REGION_IDS,
  type AggregateBucket,
  type RegionId,
} from "@shared/types";
import { regionShortLabel } from "@/lib/format";

interface RegionBalanceBarsProps {
  byRegion: Record<RegionId, AggregateBucket>;
}

// Horizontal track per region. Mean fills as a solid amber bar; peak is a
// thin amber tick further along the same track. No card border.
export function RegionBalanceBars({ byRegion }: RegionBalanceBarsProps) {
  const peakMax = Math.max(
    1e-6,
    ...REGION_IDS.map((id) => byRegion[id]?.peak ?? 0),
  );

  return (
    <section className="border-t border-line py-10">
      <div className="mb-6 flex items-end justify-between">
        <div>
          <p className="eyebrow">Region balance</p>
          <h2 className="mt-2 font-serif text-[20px] tracking-tightish text-ink-50">
            Predicted activation across cortical regions
          </h2>
        </div>
        <div className="hidden text-right text-[11px] text-ink-400 md:flex md:gap-6">
          <span>
            <span className="inline-block h-2 w-3 bg-accent align-middle" />{" "}
            mean
          </span>
          <span>
            <span className="inline-block h-3 w-[2px] bg-accent align-middle" />{" "}
            peak
          </span>
        </div>
      </div>

      <ul className="divide-y divide-line">
        {REGION_IDS.map((id) => {
          const bucket = byRegion[id] ?? { mean: 0, peak: 0 };
          const meanPct = clamp01(bucket.mean / peakMax) * 100;
          const peakPct = clamp01(bucket.peak / peakMax) * 100;
          return (
            <li
              key={id}
              className="grid grid-cols-[120px_1fr_72px_72px] items-center gap-6 py-4 text-[12px]"
            >
              <div>
                <p className="text-ink-50">{regionShortLabel(id)}</p>
                <p className="mt-1 text-[11px] text-ink-400">
                  {REGION_DESCRIPTIONS[id]}
                </p>
              </div>
              <div className="relative h-[6px] bg-line">
                <div
                  className={clsx(
                    "absolute inset-y-0 left-0 bg-accent/70",
                  )}
                  style={{ width: `${meanPct}%` }}
                />
                <div
                  className="absolute top-[-3px] h-[12px] w-[2px] bg-accent"
                  style={{ left: `calc(${peakPct}% - 1px)` }}
                  aria-hidden
                />
              </div>
              <p
                className="text-right text-ink-100"
                data-num
                title="Predicted activation, mean over watch history"
              >
                {bucket.mean.toFixed(2)}
              </p>
              <p
                className="text-right text-ink-300"
                data-num
                title="Predicted activation, peak over watch history"
              >
                {bucket.peak.toFixed(2)}
              </p>
            </li>
          );
        })}
      </ul>
      <p className="mt-4 max-w-[60ch] text-[11px] leading-relaxed text-ink-400">
        Values are unitless predicted activations from TRIBE v2; comparisons
        across regions and across videos are meaningful, absolute magnitudes
        are not.
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
