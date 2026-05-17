import clsx from "clsx";
import { type AuthorBucket } from "@shared/types";
import { regionShortLabel } from "@/lib/format";

interface AuthorPanelProps {
  // Top-N (≤20) by_author rollup from AggregateReport. May be empty
  // before any import has populated the catalogue, or contain a single
  // `author: null` bucket when the user's export uses the newer
  // `tiktokv.com/share/video/<id>/` share-shortlink URL form (no
  // @handle to extract).
  byAuthor: AuthorBucket[];
}

// "Who shows up most in your history." Three render branches:
//
//   1. Empty (no videos yet): friendly empty state pointing at /import.
//   2. Only-`null`-author bucket: explainer that share-shortlinks don't
//      carry author handles — still surfaces the bucket size + top region
//      so the slot reads as informative, not broken.
//   3. At least one named handle: ranked leaderboard (top 8) mirroring
//      the RegionLeaderboard idiom.
//
// The export name stays `AuthorPlaceholder` so existing imports keep
// working without a coordinated rename round.
export function AuthorPlaceholder({ byAuthor }: AuthorPanelProps) {
  if (byAuthor.length === 0) {
    return <EmptyState />;
  }

  const named = byAuthor.filter((a) => a.author !== null);
  const unattributed = byAuthor.find((a) => a.author === null);

  if (named.length === 0) {
    return <ShareShortlinkOnlyState bucket={unattributed!} />;
  }

  return <NamedLeaderboard named={named} unattributed={unattributed} />;
}

function EmptyState() {
  return (
    <section className="motion-fade-in border-t border-line py-10">
      <p className="eyebrow">Authors</p>
      <h2 className="mt-2 font-serif text-[20px] tracking-tightish text-ink-50">
        Who shows up most in your history
      </h2>
      <div className="mt-8 border border-dashed border-line px-6 py-10 text-center">
        <p className="font-serif text-[16px] tracking-tightish text-ink-200">
          No author data yet.
        </p>
        <p className="mx-auto mt-3 max-w-[44ch] text-[11px] leading-relaxed text-ink-400">
          Import a TikTok export to see creators ranked by how much of
          your watch history they account for and the region they engage
          most.
        </p>
      </div>
    </section>
  );
}

function ShareShortlinkOnlyState({ bucket }: { bucket: AuthorBucket }) {
  return (
    <section className="motion-fade-in border-t border-line py-10">
      <p className="eyebrow">Authors</p>
      <h2 className="mt-2 font-serif text-[20px] tracking-tightish text-ink-50">
        Who shows up most in your history
      </h2>
      <div className="mt-8 grid grid-cols-[1fr_auto] items-end gap-x-8 gap-y-3 border-t border-line pt-6">
        <p className="font-serif text-[16px] leading-snug tracking-tightish text-ink-100">
          Every video in this window came from a TikTok{" "}
          <code className="font-mono text-[14px] text-ink-200">
            tiktokv.com/share/video/&lt;id&gt;/
          </code>{" "}
          share-shortlink, which doesn't carry an author handle.
          Attribution isn't available from the export alone.
        </p>
        <p className="text-right">
          <span
            className="block font-serif text-[28px] leading-none tracking-tightish text-ink-50"
            data-num
          >
            {bucket.videos.toLocaleString()}
          </span>
          <span className="mt-2 block text-[11px] uppercase tracking-wider text-ink-400">
            unattributed videos
          </span>
        </p>
      </div>
      <p className="mt-4 max-w-[60ch] text-[11px] leading-relaxed text-ink-400">
        Top region across these videos:{" "}
        <span className="text-ink-200">{regionShortLabel(bucket.top_region)}</span>
        {" · "}mean predicted activation{" "}
        <span data-num className="text-ink-200">
          {bucket.mean_activation.toFixed(2)}
        </span>
        . Older exports (legacy{" "}
        <code className="font-mono text-[10px] text-ink-300">user_data.json</code>
        ) carry the full{" "}
        <code className="font-mono text-[10px] text-ink-300">@handle</code>{" "}
        URL form and populate this panel with a creator ranking.
      </p>
    </section>
  );
}

function NamedLeaderboard({
  named,
  unattributed,
}: {
  named: AuthorBucket[];
  unattributed: AuthorBucket | undefined;
}) {
  const visible = named.slice(0, 8);
  const videosMax = Math.max(1, ...visible.map((a) => a.videos));

  return (
    <section className="motion-fade-in border-t border-line py-10">
      <p className="eyebrow">Authors</p>
      <h2 className="mt-2 font-serif text-[20px] tracking-tightish text-ink-50">
        Who shows up most in your history
      </h2>

      <ol className="mt-8 divide-y divide-line">
        {visible.map((row, idx) => {
          const isTop = idx === 0;
          const widthPct = (row.videos / videosMax) * 100;
          return (
            <li
              key={row.author ?? "__none__"}
              className={clsx(
                "group grid grid-cols-[28px_minmax(140px,1fr)_1fr_72px_72px] items-center gap-6 py-4 text-[12px] transition-colors",
                isTop ? "bg-accent/[0.03]" : null,
              )}
              title={`Top region: ${regionShortLabel(row.top_region)} · mean activation ${row.mean_activation.toFixed(2)}`}
            >
              <span
                className={clsx(
                  "font-mono tabular-nums text-[11px]",
                  isTop ? "text-accent" : "text-ink-400",
                )}
                data-num
              >
                {(idx + 1).toString().padStart(2, "0")}
              </span>
              <div>
                <p
                  className={clsx(
                    "font-serif text-[15px] tracking-tightish",
                    isTop ? "text-ink-50" : "text-ink-100",
                  )}
                >
                  @{row.author}
                </p>
                <p className="mt-0.5 text-[11px] text-ink-400 transition-colors group-hover:text-ink-200">
                  Top region: {regionShortLabel(row.top_region)}
                </p>
              </div>
              <div className="relative h-[6px] bg-line">
                <div
                  className={clsx(
                    "absolute inset-y-0 left-0",
                    isTop ? "bg-accent" : "bg-accent/60",
                  )}
                  style={{ width: `${widthPct}%` }}
                />
              </div>
              <p
                className={clsx(
                  "text-right",
                  isTop ? "text-ink-50" : "text-ink-100",
                )}
                data-num
                title="Distinct videos in the catalogue"
              >
                {row.videos.toLocaleString()}
              </p>
              <p
                className="text-right text-ink-300"
                data-num
                title="Total watch time across rewatches"
              >
                {formatWatchTime(row.total_watch_time_s)}
              </p>
            </li>
          );
        })}
      </ol>

      <p className="mt-4 max-w-[60ch] text-[11px] leading-relaxed text-ink-400">
        Ranked by distinct videos; tie-break on watch time. Top region
        uses each creator's per-author peak — comparative ranking only,
        absolute magnitudes aren't calibrated.
        {named.length > visible.length
          ? ` (${named.length - visible.length} more authors not shown.)`
          : null}
        {unattributed
          ? ` Plus ${unattributed.videos.toLocaleString()} unattributed share-shortlink videos.`
          : null}
      </p>
    </section>
  );
}

function formatWatchTime(s: number): string {
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}
