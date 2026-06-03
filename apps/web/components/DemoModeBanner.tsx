import Link from "next/link";
import type { ReactNode } from "react";

interface DemoModeBannerProps {
  // Page-appropriate blurb describing what this curated/demo data is.
  children: ReactNode;
  // Call-to-action link out of the demo (e.g. to the live predictor).
  cta: { href: string; label: string };
}

// Banner for a view showing curated/precomputed demo data rather than a live
// result. (The single-video result panel labels mock output via the result's
// modelVersion; this labels the *data scope* — a baked, shipped sample.)
export function DemoModeBanner({ children, cta }: DemoModeBannerProps) {
  return (
    <section
      className="mb-8 flex flex-wrap items-baseline justify-between gap-x-6 gap-y-2 border-y border-accent/30 bg-accent/[0.04] px-6 py-3 text-[12px]"
      role="status"
    >
      <div className="text-ink-100">
        <span className="eyebrow mr-3 text-accent">Demo</span>
        {children}
      </div>
      <Link
        href={cta.href}
        className="text-ink-200 underline underline-offset-2 hover:text-accent"
      >
        {cta.label}
      </Link>
    </section>
  );
}
