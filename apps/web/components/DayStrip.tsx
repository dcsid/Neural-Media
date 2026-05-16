import { DAY_OF_WEEK_LABELS } from "@/lib/format";

interface DayStripProps {
  byDayOfWeek: number[];
}

// Seven cells, intensity-mapped to amber. Mon = 0 per the contract.
export function DayStrip({ byDayOfWeek }: DayStripProps) {
  const days = Array.from({ length: 7 }, (_, i) => byDayOfWeek[i] ?? 0);
  const peak = Math.max(1e-6, ...days);

  return (
    <section className="border-t border-line py-10">
      <p className="eyebrow">Day of week</p>
      <h2 className="mt-2 font-serif text-[20px] tracking-tightish text-ink-50">
        Predicted activation by weekday
      </h2>

      <ul className="mt-8 grid grid-cols-7 gap-2">
        {days.map((value, idx) => {
          const intensity = value / peak;
          return (
            <li
              key={DAY_OF_WEEK_LABELS[idx]}
              className="flex flex-col items-stretch"
              title={`${DAY_OF_WEEK_LABELS[idx]} — predicted activation ${value.toFixed(2)}`}
            >
              <div
                className="h-12 border border-line"
                style={{
                  backgroundColor: `rgba(245, 165, 36, ${Math.max(0.04, intensity * 0.7).toFixed(3)})`,
                }}
              />
              <div className="mt-2 flex items-baseline justify-between text-[11px]">
                <span className="text-ink-300">{DAY_OF_WEEK_LABELS[idx]}</span>
                <span className="text-ink-100" data-num>
                  {value.toFixed(2)}
                </span>
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
