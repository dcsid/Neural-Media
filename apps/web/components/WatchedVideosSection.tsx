import { api, ApiError, serverBaseUrl } from "@/lib/api";
import { WatchedVideosList } from "./WatchedVideosList";

// Async server component so the dashboard can stream the rest of the page
// before the videos / watch-events fetch resolves. Suspense in the parent
// shows the skeleton while we wait.

interface Props {
  // Forwarded from the dashboard so the watch-history list reads from
  // the same store the rest of the page is using (live vs demo).
  demo?: boolean;
}

export async function WatchedVideosSection({ demo = false }: Props) {
  const baseUrl = serverBaseUrl();
  const opts = { baseUrl, demo };
  try {
    const [videos, watchEvents] = await Promise.all([
      api.videos(opts),
      api.watchEvents(opts),
    ]);
    return <WatchedVideosList videos={videos} watchEvents={watchEvents} demo={demo} />;
  } catch (err) {
    if (err instanceof ApiError) {
      // The dashboard parent already handles offline; here we degrade
      // softly without taking the whole page down.
      return (
        <section className="border-t border-line py-10 text-[12px] text-ink-400">
          <p className="eyebrow">Watch history</p>
          <p className="mt-2">
            Could not load the watch history list. Reload to retry.
          </p>
        </section>
      );
    }
    throw err;
  }
}
