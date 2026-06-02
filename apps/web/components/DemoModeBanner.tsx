import Link from "next/link";
import type { ReactNode } from "react";

interface DemoModeBannerProps {
  // Page-appropriate blurb. Defaults to the dashboard copy (a curated slice
  // of one user's history) so existing call sites render unchanged.
  children?: ReactNode;
  // Override the default call-to-action link.
  cta?: { href: string; label: string };
}

// Banner rendered on any view showing curated/demo data rather than the
// user's own catalogue. Distinct from MockModeBadge — that one labels the
// inference *source* (mock vs real backend); this one labels the data
// *scope* (demo curated sample vs the user's own catalogue).
//
// Both can be visible at once and shouldn't overlap visually: this lives
// inline at the top of the main content, MockModeBadge lives in the global
// header from layout.tsx.
export function DemoModeBanner({ children, cta }: DemoModeBannerProps) {
  const action = cta ?? { href: "/import", label: "Import your own export →" };
  return (
    <section
      className="mb-8 flex flex-wrap items-baseline justify-between gap-x-6 gap-y-2 border-y border-accent/30 bg-accent/[0.04] px-6 py-3 text-[12px]"
      role="status"
    >
      <div className="text-ink-100">
        <span className="eyebrow mr-3 text-accent">Demo</span>
        {children ?? (
          <>
            Browsing a curated 7-minute slice of one user&apos;s TikTok
            history, ingested through the full pipeline in mock mode.
          </>
        )}
      </div>
      <Link
        href={action.href}
        className="text-ink-200 underline underline-offset-2 hover:text-accent"
      >
        {action.label}
      </Link>
    </section>
  );
}
