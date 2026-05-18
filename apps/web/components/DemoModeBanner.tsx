import Link from "next/link";

// Sticky banner rendered at the top of any page being viewed in demo
// mode (`?demo=1`). Distinct from MockModeBadge — that one labels the
// inference *source* (mock vs real backend); this one labels the data
// *scope* (demo curated sample vs the user's own catalogue).
//
// Both can be visible at once and shouldn't overlap visually: this lives
// inline at the top of the main content, MockModeBadge lives in the
// global header from layout.tsx.
export function DemoModeBanner() {
  return (
    <section
      className="mb-8 flex flex-wrap items-baseline justify-between gap-x-6 gap-y-2 border-y border-accent/30 bg-accent/[0.04] px-6 py-3 text-[12px]"
      role="status"
    >
      <div className="text-ink-100">
        <span className="eyebrow mr-3 text-accent">Demo</span>
        Browsing a curated 7-minute slice of one user&apos;s TikTok history,
        ingested through the full pipeline in mock mode.
      </div>
      <Link
        href="/import"
        className="text-ink-200 underline underline-offset-2 hover:text-accent"
      >
        Import your own export →
      </Link>
    </section>
  );
}
