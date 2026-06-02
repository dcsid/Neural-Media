import Link from "next/link";
import type { VideoMetadata, WatchEvent } from "@shared/types";
import { formatDurationSeconds, videoTitle } from "@/lib/format";

interface WatchedVideosListProps {
  videos: VideoMetadata[];
  watchEvents: WatchEvent[];
  // Pass-through: when true, video-detail links carry `?demo=1` so the
  // user stays in the demo store after clicking through.
  demo?: boolean;
}

interface Row {
  video: VideoMetadata;
  lastWatchedAt: string | null;
  watchCount: number;
}

export function WatchedVideosList({
  videos,
  watchEvents,
  demo = false,
}: WatchedVideosListProps) {
  const rows = buildRows(videos, watchEvents);
  const demoSuffix = demo ? "?demo=1" : "";

  return (
    <section className="border-t border-line py-10">
      <p className="eyebrow">Watch history</p>
      <h2 className="mt-2 font-serif text-[20px] tracking-tightish text-ink-50">
        Videos in this snapshot
      </h2>

      {rows.length === 0 ? (
        <p className="mt-6 text-[13px] text-ink-300">
          No videos have been ingested yet. Drop a TikTok history export into{" "}
          <code className="font-mono text-ink-100">data/raw/</code> and run the
          pipeline.
        </p>
      ) : (
        <ul className="mt-6 divide-y divide-line text-[13px]">
          {rows.map(({ video, lastWatchedAt, watchCount }) => (
            <li key={video.id} className="grid grid-cols-[1fr_auto_auto_auto] items-baseline gap-6 py-4">
              <div className="min-w-0">
                <Link
                  href={`/v/${video.id}${demoSuffix}`}
                  title={videoTitle(video)}
                  className="block truncate text-ink-50 hover:text-accent"
                >
                  {videoTitle(video)}
                </Link>
                <p
                  className="mt-1 truncate text-[11px] text-ink-400"
                  title={video.author ? `@${video.author}` : video.source_url}
                >
                  {video.author ? `@${video.author}` : video.source_url}
                </p>
              </div>
              <span className="text-right text-[11px] text-ink-300" data-num>
                {formatDurationSeconds(video.duration_s)}
              </span>
              <span className="text-right text-[11px] text-ink-300" data-num>
                {watchCount > 0 ? `${watchCount}×` : "—"}
              </span>
              <span className="text-right text-[11px] text-ink-400" data-num>
                {formatRelativeWatchedAt(lastWatchedAt)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function buildRows(videos: VideoMetadata[], events: WatchEvent[]): Row[] {
  const byVideo = new Map<string, { count: number; latest: string | null }>();
  for (const e of events) {
    const entry = byVideo.get(e.video_id) ?? { count: 0, latest: null };
    entry.count += 1;
    if (!entry.latest || new Date(e.watched_at) > new Date(entry.latest)) {
      entry.latest = e.watched_at;
    }
    byVideo.set(e.video_id, entry);
  }
  return videos
    .map((video) => {
      const entry = byVideo.get(video.id);
      return {
        video,
        lastWatchedAt: entry?.latest ?? null,
        watchCount: entry?.count ?? 0,
      };
    })
    .sort((a, b) => {
      const at = a.lastWatchedAt ? new Date(a.lastWatchedAt).getTime() : 0;
      const bt = b.lastWatchedAt ? new Date(b.lastWatchedAt).getTime() : 0;
      return bt - at;
    });
}

function formatRelativeWatchedAt(iso: string | null): string {
  if (!iso) return "—";
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return "—";
  const fmt = new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
  return fmt.format(t);
}
