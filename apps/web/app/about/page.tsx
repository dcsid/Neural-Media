import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "About — Neural Media",
  description:
    "What Neural Media does and does not measure. Predicted average cortical fMRI response from Meta FAIR TRIBE v2. Local-first, non-commercial.",
};

// Plain-text framing page. Mirrors docs/scientific-framing.md so the
// constraints that govern in-app copy are surfaced to the user.
export default function AboutPage() {
  return (
    <main className="mx-auto max-w-[1280px] px-8 pb-10 pt-12">
      <p className="eyebrow mb-4">About</p>
      <h1 className="font-serif text-[32px] leading-[1.15] tracking-tightish text-ink-50">
        What Neural Media does — and what it doesn&apos;t.
      </h1>
      <p className="mt-4 max-w-[60ch] text-[13px] leading-relaxed text-ink-200">
        Neural Media runs every video in your local TikTok watch history
        through Meta FAIR&apos;s TRIBE v2 model to predict the{" "}
        <span className="text-ink-50">
          predicted average cortical response
        </span>{" "}
        across the 720 subjects TRIBE was trained on. The four sections
        below are the framing the rest of the app is built against. If a
        chart label or tooltip drifts from this language, the chart is
        wrong, not the page.
      </p>

      <section className="mt-12 grid gap-x-12 gap-y-10 border-t border-line pt-10 md:grid-cols-2">
        <div>
          <p className="eyebrow mb-3">What it DOES measure</p>
          <ul className="space-y-3 text-[13px] leading-relaxed text-ink-200">
            <li>
              <span className="text-ink-50">Predicted BOLD fMRI response</span>{" "}
              on a 20,484-vertex cortical surface, averaged across TRIBE
              v2&apos;s training subjects.
            </li>
            <li>
              Predicted engagement in{" "}
              <span className="text-ink-50">well-localized cortical regions</span>
              : visual cortex, auditory cortex, language network,
              face-selective regions.
            </li>
            <li>
              Divergence and similarity{" "}
              <span className="text-ink-50">between videos</span> — content
              clustered by predicted neural fingerprint.
            </li>
            <li>
              Aggregate patterns across the watch history: time-of-day
              shifts, content-type breakdowns, session-level variation.
            </li>
          </ul>
        </div>

        <div>
          <p className="eyebrow mb-3">What it does NOT measure</p>
          <ul className="space-y-3 text-[13px] leading-relaxed text-ink-200">
            <li>
              <span className="text-ink-50">Your individual brain.</span> The
              model predicts an average, not a person-specific response.
            </li>
            <li>
              <span className="text-ink-50">
                Subcortical reward / addiction circuitry.
              </span>{" "}
              Nucleus accumbens, VTA, amygdala, and friends are not in the
              output space.
            </li>
            <li>
              <span className="text-ink-50">
                Cumulative effects, habituation, or neuroplasticity
              </span>{" "}
              over time.
            </li>
            <li>
              <span className="text-ink-50">Subjective states</span> —
              aesthetic preference, confusion, cognitive overload, memory
              encoding, satisfaction.
            </li>
          </ul>
        </div>
      </section>

      <section className="mt-12 grid gap-x-12 gap-y-10 border-t border-line pt-10 md:grid-cols-2">
        <div>
          <p className="eyebrow mb-3">License — CC-BY-NC 4.0</p>
          <p className="text-[13px] leading-relaxed text-ink-200">
            Neural Media is distributed under Creative Commons{" "}
            <span className="text-ink-50">Attribution-NonCommercial 4.0</span>,
            inherited from Meta FAIR&apos;s TRIBE v2 weights. Personal,
            research, and educational use is welcome. Commercial use is not.
          </p>
          <p className="mt-3 text-[12px] leading-relaxed text-ink-300">
            See{" "}
            <a
              href="https://creativecommons.org/licenses/by-nc/4.0/"
              rel="noreferrer noopener"
              target="_blank"
              className="text-ink-100 underline-offset-2 hover:text-accent hover:underline focus:text-accent focus:underline"
            >
              creativecommons.org/licenses/by-nc/4.0
            </a>{" "}
            for the full license text.
          </p>
        </div>

        <div>
          <p className="eyebrow mb-3">Privacy</p>
          <ul className="space-y-3 text-[13px] leading-relaxed text-ink-200">
            <li>The pipeline runs entirely on-device.</li>
            <li>Your watch history never leaves the machine.</li>
            <li>
              No analytics SDKs, no telemetry, no remote logging. The app
              makes no outbound requests beyond your local API.
            </li>
            <li>
              Downloaded videos live in{" "}
              <code className="font-mono text-ink-100">data/videos/</code>{" "}
              and are git-ignored.
            </li>
          </ul>
        </div>
      </section>

      <section className="mt-12 border-t border-line pt-6 text-[11px] leading-relaxed text-ink-400">
        <p>
          Reproducibility envelope: every inference run logs model id +
          version, random seed, preprocessing params, configuration hash, and
          a wall-clock UTC timestamp. The mock backend obeys the same
          envelope so the contract is exercised end-to-end before TRIBE
          itself arrives.
        </p>
      </section>
    </main>
  );
}
