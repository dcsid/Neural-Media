import Link from "next/link";
import { notFound } from "next/navigation";
import { api, ApiError, serverBaseUrl } from "@/lib/api";
import { formatDurationSeconds, videoTitle } from "@/lib/format";
import { ApiOfflineState } from "@/components/ApiOfflineState";
import { BrainMeshPlaceholder } from "@/components/BrainMeshPlaceholder";
import { RegionReadingsTable } from "@/components/RegionReadingsTable";
import { StatRow } from "@/components/StatRow";

export const dynamic = "force-dynamic";

interface PageProps {
  params: Promise<{ id: string }>;
}

export default async function VideoDetailPage({ params }: PageProps) {
  const { id } = await params;
  const baseUrl = serverBaseUrl();
  try {
    const [video, metrics] = await Promise.all([
      api.video(id, { baseUrl }),
      api.videoMetrics(id, { baseUrl }),
    ]);

    const meanAvg = average(metrics.map((m) => m.mean));
    const peakAcross = Math.max(0, ...metrics.map((m) => m.peak));
    const peakRegion = metrics.reduce<typeof metrics[number] | null>(
      (best, m) => (best && best.peak >= m.peak ? best : m),
      null,
    );

    return (
      <main className="mx-auto max-w-[1280px] px-8 pb-10 pt-12">
        <Link
          href="/"
          className="eyebrow inline-block transition-colors hover:text-ink-100"
        >
          ← Dashboard
        </Link>

        <section className="mt-6 grid gap-12 md:grid-cols-[1.1fr_1fr] md:items-start">
          <div>
            <p className="eyebrow mb-4">Video detail</p>
            <h1 className="font-serif text-[32px] leading-[1.15] tracking-tightish text-ink-50">
              {videoTitle(video)}
            </h1>
            <p className="mt-3 text-[13px] text-ink-300">
              {video.author ? `@${video.author} · ` : ""}
              <a
                href={video.source_url}
                rel="noreferrer noopener"
                target="_blank"
                className="break-all underline-offset-2 hover:text-ink-100 hover:underline"
              >
                {video.source_url}
              </a>
            </p>

            <div className="mt-8">
              <StatRow
                items={[
                  {
                    label: "Duration",
                    value: formatDurationSeconds(video.duration_s),
                  },
                  {
                    label: "Mean activation",
                    value: meanAvg.toFixed(2),
                    hint: "averaged across regions",
                  },
                  {
                    label: "Peak activation",
                    value: peakAcross.toFixed(2),
                    hint: peakRegion
                      ? `in ${peakRegion.region_id.toUpperCase()}`
                      : undefined,
                  },
                  {
                    label: "Tags",
                    value: video.tags.length ? video.tags.length.toString() : "—",
                    hint: video.tags.slice(0, 3).join(", ") || undefined,
                  },
                ]}
              />
            </div>
          </div>
          <div>
            <BrainMeshPlaceholder label="placeholder — cortical mesh (timeline-driven)" />
            <p className="mt-3 text-[11px] text-ink-400">
              Scrubber slot. Once brain-viz lands, the timeline below this
              area drives the predicted-activation heatmap on the mesh.
            </p>
          </div>
        </section>

        <RegionReadingsTable metrics={metrics} />

        <section className="border-t border-line py-10 text-[11px] leading-relaxed text-ink-400">
          <p className="eyebrow mb-2">Reading the numbers</p>
          <p className="max-w-[70ch]">
            <span className="text-ink-200">Mean</span> is the time-averaged
            predicted activation for the region over the video.{" "}
            <span className="text-ink-200">Peak</span> is the highest
            instantaneous predicted activation.{" "}
            <span className="text-ink-200">Sustained</span> is the mean of the
            top-quartile timepoints — useful when peak is a single spike. All
            values are TRIBE v2 predictions of the average cortical response;
            absolute magnitudes are uncalibrated, comparisons across videos
            and regions are the supported reading.
          </p>
        </section>
      </main>
    );
  } catch (err) {
    if (err instanceof ApiError) {
      if (err.kind === "http" && err.status === 404) notFound();
      return <ApiOfflineState url={err.url} message={err.message} />;
    }
    throw err;
  }
}

function average(values: number[]): number {
  if (values.length === 0) return 0;
  let sum = 0;
  for (const v of values) sum += v;
  return sum / values.length;
}
