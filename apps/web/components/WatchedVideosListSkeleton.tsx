// Lightweight skeleton matching the proportions of WatchedVideosList rows.
// Pure CSS / no client JS — survives streaming.

const ROWS = Array.from({ length: 5 });

export function WatchedVideosListSkeleton() {
  return (
    <section
      className="border-t border-line py-10"
      aria-busy="true"
      aria-live="polite"
    >
      <p className="eyebrow">Watch history</p>
      <h2 className="mt-2 font-serif text-[20px] tracking-tightish text-ink-50">
        Videos in this snapshot
      </h2>

      <ul className="mt-6 divide-y divide-line">
        {ROWS.map((_, i) => (
          <li
            key={i}
            className="grid grid-cols-[1fr_auto_auto_auto] items-baseline gap-6 py-4 text-[13px]"
          >
            <div className="min-w-0">
              <div className="h-[14px] w-[60%] animate-pulse bg-line" />
              <div className="mt-2 h-[11px] w-[35%] animate-pulse bg-line/70" />
            </div>
            <div className="h-[11px] w-12 animate-pulse bg-line/70" />
            <div className="h-[11px] w-8 animate-pulse bg-line/70" />
            <div className="h-[11px] w-20 animate-pulse bg-line/70" />
          </li>
        ))}
      </ul>
      <p className="sr-only">Loading watch history…</p>
    </section>
  );
}
