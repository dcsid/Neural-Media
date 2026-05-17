// Slot for the "by-author" panel. The backing data —
// AggregateReport.by_author — isn't in the contract yet; this placeholder
// keeps the dashboard rhythm intact and signals what's coming. See
// docs/worker-briefs/aggregate-by-author-proposal.md.
export function AuthorPlaceholder() {
  return (
    <section className="motion-fade-in border-t border-line py-10">
      <p className="eyebrow">Authors</p>
      <h2 className="mt-2 font-serif text-[20px] tracking-tightish text-ink-50">
        Who shows up most in your history
      </h2>

      <div className="mt-8 border border-dashed border-line px-6 py-10 text-center">
        <p className="font-serif text-[16px] tracking-tightish text-ink-200">
          Coming when{" "}
          <code className="font-mono text-[14px] text-ink-100">
            AggregateReport.by_author
          </code>{" "}
          lands.
        </p>
        <p className="mt-3 max-w-[44ch] text-[11px] leading-relaxed text-ink-400 mx-auto">
          The slot will rank creators by total predicted activation and the
          region they engage most. Proposal sent to the integration lead.
        </p>
      </div>
    </section>
  );
}
