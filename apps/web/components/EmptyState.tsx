import Link from "next/link";
import type { ReactNode } from "react";

interface EmptyStateProps {
  eyebrow: string;
  headline: string;
  // First paragraph — explanatory, no marketing.
  children: ReactNode;
  // Optional CTA region (typically one or two labelled code blocks).
  cta?: ReactNode;
  // Optional diagnostic block at the bottom, mirroring ApiOfflineState.
  diagnostic?: ReactNode;
}

// Shared visual shell for the app's no-data states. Matches ApiOfflineState:
// eyebrow + serif headline + explanatory paragraph + a CTA region. No card
// borders, no rounded-2xl. Keeps the three views from looking like errors.
export function EmptyState({
  eyebrow,
  headline,
  children,
  cta,
  diagnostic,
}: EmptyStateProps) {
  return (
    <main className="mx-auto max-w-[1280px] px-8 py-16">
      <p className="eyebrow mb-3">{eyebrow}</p>
      <h1 className="font-serif text-[32px] tracking-tightish text-ink-50">
        {headline}
      </h1>
      <div className="mt-4 max-w-[60ch] text-[13px] leading-relaxed text-ink-200">
        {children}
      </div>

      {cta && <div className="mt-10 grid gap-10 md:grid-cols-2">{cta}</div>}

      {diagnostic && (
        <div className="mt-12 border-t border-line pt-6 text-[11px] text-ink-400">
          {diagnostic}
        </div>
      )}
    </main>
  );
}

interface EmptyStateStepProps {
  index: number;
  label: string;
  command: string;
  hint: ReactNode;
}

export function EmptyStateStep({ index, label, command, hint }: EmptyStateStepProps) {
  return (
    <section>
      <p className="eyebrow mb-2">
        {index}. {label}
      </p>
      <pre className="overflow-x-auto border border-line bg-surface/60 px-4 py-3 font-mono text-[12px] text-ink-100">
        <code>{command}</code>
      </pre>
      <p className="mt-3 max-w-[40ch] text-[12px] leading-relaxed text-ink-300">
        {hint}
      </p>
    </section>
  );
}

interface EmptyStatePrimaryCtaProps {
  index: number;
  label: string;
  href: string;
  heading: string;
  hint: ReactNode;
}

// Prominent primary action card. Same vertical rhythm as
// EmptyStateStep, but renders an amber-bordered Next link instead of a
// code block. Used by NoVideosState to surface the /import drop-zone.
export function EmptyStatePrimaryCta({
  index,
  label,
  href,
  heading,
  hint,
}: EmptyStatePrimaryCtaProps) {
  return (
    <section>
      <p className="eyebrow mb-2">
        {index}. {label}
      </p>
      <Link
        href={href}
        className="group flex items-center justify-between border border-accent/60 bg-accent/[0.04] px-4 py-3 font-serif text-[16px] tracking-tightish text-ink-50 transition-colors hover:border-accent hover:bg-accent/10 focus:border-accent focus:bg-accent/10"
      >
        <span>{heading}</span>
        <span
          aria-hidden
          className="font-mono text-[12px] text-accent transition-transform group-hover:translate-x-0.5"
        >
          →
        </span>
      </Link>
      <p className="mt-3 max-w-[40ch] text-[12px] leading-relaxed text-ink-300">
        {hint}
      </p>
    </section>
  );
}
