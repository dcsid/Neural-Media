interface StatRowProps {
  items: { label: string; value: string; hint?: string }[];
}

export function StatRow({ items }: StatRowProps) {
  return (
    <dl className="grid grid-cols-2 gap-x-6 gap-y-4 border-t border-line pt-6 md:grid-cols-4">
      {items.map((item) => (
        <div key={item.label}>
          <dt className="eyebrow">{item.label}</dt>
          <dd className="mt-2">
            <span
              className="font-serif text-[24px] tracking-tightish text-ink-50"
              data-num
            >
              {item.value}
            </span>
            {item.hint && (
              <span className="mt-1 block text-[11px] text-ink-400">
                {item.hint}
              </span>
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}
