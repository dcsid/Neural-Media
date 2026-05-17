import Link from "next/link";

export default function VideoNotFound() {
  return (
    <main className="mx-auto max-w-[1280px] px-8 py-16">
      <p className="eyebrow mb-3">Video detail</p>
      <h1 className="font-serif text-[32px] tracking-tightish text-ink-50">
        That video isn&apos;t in this catalogue.
      </h1>
      <p className="mt-4 max-w-[60ch] text-[13px] leading-relaxed text-ink-200">
        The id in the URL doesn&apos;t match any video Neural Media has
        ingested. It may have been pruned, or the link is from a different
        local install.
      </p>
      <p className="mt-6">
        <Link
          href="/"
          className="text-[13px] text-ink-100 underline-offset-2 hover:text-accent hover:underline focus:text-accent focus:underline focus:outline-none"
        >
          ← Back to the dashboard
        </Link>
      </p>
    </main>
  );
}
