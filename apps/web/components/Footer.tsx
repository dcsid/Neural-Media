import Link from "next/link";

export function Footer() {
  return (
    <footer className="mt-24 border-t border-line">
      <div className="mx-auto grid max-w-[1280px] gap-6 px-8 py-10 text-[12px] text-ink-300 md:grid-cols-[1.4fr_1fr_auto]">
        <div className="max-w-[60ch]">
          <p className="eyebrow mb-2">Framing</p>
          <p className="leading-relaxed">
            Neural Media predicts the{" "}
            <span className="text-ink-100">
              average cortical response across TRIBE v2&apos;s 720 training
              subjects
            </span>
            , not your individual brain. The model covers cortical surface only
            — subcortical reward circuitry, habituation, and subjective
            experience are out of scope.
          </p>
        </div>
        <div className="max-w-[60ch]">
          <p className="eyebrow mb-2">Non-commercial</p>
          <p className="leading-relaxed">
            Built on Meta FAIR TRIBE v2, distributed under CC-BY-NC 4.0. No
            account, no tracking: we analyze only the segment you choose and keep
            just the prediction. No analytics, no telemetry.
          </p>
        </div>
        <nav className="flex flex-col gap-2 md:items-end" aria-label="Footer">
          <p className="eyebrow mb-2">More</p>
          <Link
            href="/about"
            className="text-ink-100 underline-offset-2 hover:text-accent hover:underline"
          >
            About &amp; framing
          </Link>
        </nav>
      </div>
    </footer>
  );
}
