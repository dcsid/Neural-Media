import clsx from "clsx";
import { formatHour } from "@/lib/format";

interface HourHistogramProps {
  byHourOfDay: number[];
}

// 24 vertical bars. Honest hours-of-day. Tooltip uses "predicted
// activation" — never "engagement" without qualification.
export function HourHistogram({ byHourOfDay }: HourHistogramProps) {
  const bars = Array.from({ length: 24 }, (_, i) => byHourOfDay[i] ?? 0);
  const peak = Math.max(1e-6, ...bars);

  return (
    <section className="border-t border-line py-10">
      <p className="eyebrow">Hour of day</p>
      <h2 className="mt-2 font-serif text-[20px] tracking-tightish text-ink-50">
        Predicted activation by hour
      </h2>

      <div className="mt-8 grid items-end gap-[3px]" style={gridStyle}>
        {bars.map((value, hour) => {
          const heightPct = (value / peak) * 100;
          return (
            <div
              key={hour}
              className="flex flex-col items-center"
              title={`${formatHour(hour)} — predicted activation ${value.toFixed(2)}`}
            >
              <div className="flex h-[80px] w-full items-end">
                <div
                  className={clsx(
                    "w-full",
                    value > 0 ? "bg-accent/70" : "bg-line",
                  )}
                  style={{ height: `${Math.max(2, heightPct)}%` }}
                />
              </div>
            </div>
          );
        })}
      </div>
      <div className="mt-2 grid text-[10px] text-ink-400" style={gridStyle}>
        {bars.map((_, hour) => (
          <div key={hour} className="text-center">
            {hour % 6 === 0 ? formatHour(hour) : ""}
          </div>
        ))}
      </div>
    </section>
  );
}

const gridStyle = { gridTemplateColumns: "repeat(24, minmax(0, 1fr))" } as const;
