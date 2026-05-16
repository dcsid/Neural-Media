import { REGION_IDS, type RegionId, type RegionMetrics } from "@shared/types";
import { api, ApiError, serverBaseUrl } from "@/lib/api";
import { ApiOfflineState } from "@/components/ApiOfflineState";
import { ComparePicker } from "@/components/ComparePicker";
import { CompareRegionRow } from "@/components/CompareRegionRow";
import { videoTitle } from "@/lib/format";

export const dynamic = "force-dynamic";

interface PageProps {
  searchParams: Promise<{ a?: string; b?: string }>;
}

export default async function ComparePage({ searchParams }: PageProps) {
  const { a, b } = await searchParams;
  const baseUrl = serverBaseUrl();

  try {
    const videos = await api.videos({ baseUrl });
    const haveBoth = !!a && !!b && a !== b;

    const [metricsA, metricsB] = haveBoth
      ? await Promise.all([
          api.videoMetrics(a!, { baseUrl }).catch(swallow404),
          api.videoMetrics(b!, { baseUrl }).catch(swallow404),
        ])
      : [null, null];

    const videoA = videos.find((v) => v.id === a) ?? null;
    const videoB = videos.find((v) => v.id === b) ?? null;

    const mapA = indexByRegion(metricsA ?? []);
    const mapB = indexByRegion(metricsB ?? []);
    const domainMax = Math.max(
      1e-6,
      ...(metricsA?.flatMap((m) => m.timeseries) ?? []),
      ...(metricsB?.flatMap((m) => m.timeseries) ?? []),
    );

    return (
      <main className="mx-auto max-w-[1280px] px-8 pb-10 pt-12">
        <p className="eyebrow mb-4">Compare</p>
        <h1 className="font-serif text-[32px] tracking-tightish text-ink-50">
          Predicted activation, side by side.
        </h1>
        <p className="mt-3 max-w-[60ch] text-[13px] leading-relaxed text-ink-200">
          Pick any two videos to compare per-region timelines. The sparklines
          share a y-axis so peaks and shapes are directly comparable. The
          difference column (<span className="text-ink-50">A − B</span>) is on
          predicted-activation means.
        </p>

        <ComparePicker
          videos={videos}
          selectedA={a ?? null}
          selectedB={b ?? null}
        />

        {haveBoth ? (
          <section className="mt-4">
            <header className="grid grid-cols-[120px_1fr_1fr_88px] items-baseline gap-6 border-b border-line pb-3 text-[11px] text-ink-400">
              <span className="eyebrow">Region</span>
              <span className="eyebrow">A · {videoA ? videoTitle(videoA) : "—"}</span>
              <span className="eyebrow">B · {videoB ? videoTitle(videoB) : "—"}</span>
              <span className="eyebrow text-right">Δ mean</span>
            </header>
            <ul>
              {REGION_IDS.map((id) => (
                <CompareRegionRow
                  key={id}
                  regionId={id}
                  a={mapA.get(id)}
                  b={mapB.get(id)}
                  domainMax={domainMax}
                />
              ))}
            </ul>
          </section>
        ) : (
          <section className="mt-8 border-t border-line pt-10">
            <p className="text-[13px] text-ink-300">
              Select two videos above to render the side-by-side timelines.
            </p>
          </section>
        )}
      </main>
    );
  } catch (err) {
    if (err instanceof ApiError) {
      return <ApiOfflineState url={err.url} message={err.message} />;
    }
    throw err;
  }
}

function indexByRegion(metrics: RegionMetrics[]) {
  const m = new Map<RegionId, RegionMetrics>();
  for (const x of metrics) m.set(x.region_id, x);
  return m;
}

function swallow404(err: unknown): RegionMetrics[] | null {
  if (err instanceof ApiError && err.kind === "http" && err.status === 404) {
    return null;
  }
  throw err;
}
