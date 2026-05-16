interface StatRowProps {
  items: { label: string; value: string; hint?: string }[];
}

export function StatRow({ items }: StatRowProps) {
  return (
    <dl className="grid grid-cols-2 gap-x-6 gap-y-4 border-t border-line pt-6 md:grid-cols-4">
      {items.map((item) => (
        <div key={item.label}>
          <dt className="eyebrow">{item.label}</dt>
          <dd
            className="mt-2 font-serif text-[24px] tracking-tightish text-ink-50"
            data-num
          >
            {item.value}
          </dd>
          {item.hint && (
            <p className="mt-1 text-[11px] text-ink-400">{item.hint}</p>
          )}
        </div>
      ))}
    </dl>
  );
}
