import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "About — Neural Media",
  description:
    "What Neural Media does and does not measure. Predicted average cortical fMRI response to a YouTube video segment, from Meta FAIR TRIBE v2. Non-commercial.",
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
        Neural Media runs a short YouTube video segment through Meta
        FAIR&apos;s TRIBE v2 model to predict the{" "}
        <span className="text-ink-50">
          predicted average cortical response
        </span>{" "}
        across the 720 subjects TRIBE was trained on. The four sections
        below spell out exactly what the numbers in this app mean — and,
        just as importantly, what they don&apos;t.
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
              How predicted engagement{" "}
              <span className="text-ink-50">rises and falls across the segment</span>{" "}
              — a per-region time-course, not just a single number.
            </li>
            <li>
              Differences <span className="text-ink-50">between clips</span> —
              compare what you paste against the curated gallery examples.
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
            <li>
              <span className="text-ink-50">
                No account, no sign-in, no tracking
              </span>{" "}
              — paste a link, get a prediction.
            </li>
            <li>
              Analysis runs on a cloud GPU that fetches{" "}
              <span className="text-ink-50">only your chosen segment</span> to a
              temporary directory it deletes after inference — the full video is
              never stored.
            </li>
            <li>
              Only the small result (the predicted region activations) persists
              — in AWS S3 / DynamoDB — and auto-expires.
            </li>
            <li>
              No analytics SDKs, no telemetry. The curated gallery is
              precomputed static JSON: no inference, no network.
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
