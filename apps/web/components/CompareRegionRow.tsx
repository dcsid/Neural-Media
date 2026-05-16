import {
  REGION_DESCRIPTIONS,
  type RegionId,
  type RegionMetrics,
} from "@shared/types";
import { regionShortLabel } from "@/lib/format";
import { Sparkline } from "./Sparkline";

interface CompareRegionRowProps {
  regionId: RegionId;
  a: RegionMetrics | undefined;
  b: RegionMetrics | undefined;
  domainMax: number;
}

export function CompareRegionRow({
  regionId,
  a,
  b,
  domainMax,
}: CompareRegionRowProps) {
  const diff = (a?.mean ?? 0) - (b?.mean ?? 0);
  const diffSign = diff > 0 ? "+" : diff < 0 ? "" : "";
  const diffClass =
    Math.abs(diff) < 0.005
      ? "text-ink-300"
      : diff > 0
        ? "text-accent"
        : "text-ink-100";

  return (
    <li className="grid grid-cols-[120px_1fr_1fr_88px] items-center gap-6 border-b border-line py-4 text-[12px]">
      <div>
        <p className="text-ink-50">{regionShortLabel(regionId)}</p>
        <p className="mt-1 text-[11px] text-ink-400">
          {REGION_DESCRIPTIONS[regionId]}
        </p>
      </div>
      <div className="text-ink-100">
        <Sparkline
          values={a?.timeseries ?? []}
          domainMax={domainMax}
          ariaLabel={`Video A — predicted activation, ${REGION_DESCRIPTIONS[regionId]}`}
          width={260}
        />
        <p className="mt-1 text-[11px] text-ink-400" data-num>
          mean {a ? a.mean.toFixed(2) : "—"} · peak {a ? a.peak.toFixed(2) : "—"}
        </p>
      </div>
      <div className="text-ink-100">
        <Sparkline
          values={b?.timeseries ?? []}
          domainMax={domainMax}
          ariaLabel={`Video B — predicted activation, ${REGION_DESCRIPTIONS[regionId]}`}
          width={260}
        />
        <p className="mt-1 text-[11px] text-ink-400" data-num>
          mean {b ? b.mean.toFixed(2) : "—"} · peak {b ? b.peak.toFixed(2) : "—"}
        </p>
      </div>
      <div className="text-right">
        <p className="eyebrow mb-1">A − B</p>
        <p className={`font-serif text-[18px] ${diffClass}`} data-num>
          {diffSign}
          {diff.toFixed(2)}
        </p>
      </div>
    </li>
  );
}
