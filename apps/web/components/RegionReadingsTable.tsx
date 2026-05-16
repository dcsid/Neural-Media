import {
  REGION_DESCRIPTIONS,
  REGION_IDS,
  type RegionId,
  type RegionMetrics,
} from "@shared/types";
import { regionShortLabel } from "@/lib/format";
import { Sparkline } from "./Sparkline";

interface RegionReadingsTableProps {
  metrics: RegionMetrics[];
}

export function RegionReadingsTable({ metrics }: RegionReadingsTableProps) {
  const byRegion = new Map<RegionId, RegionMetrics>();
  for (const m of metrics) byRegion.set(m.region_id, m);

  const peakDomain = Math.max(
    1e-6,
    ...metrics.flatMap((m) => m.timeseries),
  );

  return (
    <section className="border-t border-line py-10">
      <p className="eyebrow">Region readings</p>
      <h2 className="mt-2 font-serif text-[20px] tracking-tightish text-ink-50">
        Predicted activation, by region
      </h2>

      <div className="mt-6 overflow-x-auto">
        <table className="w-full border-collapse text-[12px]">
          <thead>
            <tr className="text-left text-[11px] text-ink-400">
              <th className="border-b border-line py-2 pr-4 font-normal">Region</th>
              <th className="border-b border-line py-2 pr-4 font-normal">Description</th>
              <th className="border-b border-line py-2 pr-4 text-right font-normal">Mean</th>
              <th className="border-b border-line py-2 pr-4 text-right font-normal">Peak</th>
              <th className="border-b border-line py-2 pr-4 text-right font-normal">Sustained</th>
              <th className="border-b border-line py-2 font-normal">Timeseries</th>
            </tr>
          </thead>
          <tbody>
            {REGION_IDS.map((id) => {
              const m = byRegion.get(id);
              return (
                <tr key={id} className="align-middle">
                  <td className="border-b border-line py-3 pr-4 text-ink-50">
                    {regionShortLabel(id)}
                  </td>
                  <td className="border-b border-line py-3 pr-4 text-ink-300">
                    {REGION_DESCRIPTIONS[id]}
                  </td>
                  <td
                    className="border-b border-line py-3 pr-4 text-right text-ink-100"
                    data-num
                    title="Predicted activation, mean over video"
                  >
                    {m ? m.mean.toFixed(2) : "—"}
                  </td>
                  <td
                    className="border-b border-line py-3 pr-4 text-right text-accent"
                    data-num
                    title="Predicted activation, peak"
                  >
                    {m ? m.peak.toFixed(2) : "—"}
                  </td>
                  <td
                    className="border-b border-line py-3 pr-4 text-right text-ink-200"
                    data-num
                    title="Predicted activation, sustained (mean of top-quartile timepoints)"
                  >
                    {m ? m.sustained.toFixed(2) : "—"}
                  </td>
                  <td className="border-b border-line py-3 text-ink-100">
                    <Sparkline
                      values={m?.timeseries ?? []}
                      domainMax={peakDomain}
                      ariaLabel={`Predicted activation timeseries — ${REGION_DESCRIPTIONS[id]}`}
                    />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
