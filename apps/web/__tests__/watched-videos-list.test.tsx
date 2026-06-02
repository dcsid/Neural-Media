import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import type { VideoMetadata } from "@shared/types";
import { videoTitle } from "@/lib/format";
import { WatchedVideosList } from "@/components/WatchedVideosList";

function video(over: Partial<VideoMetadata> = {}): VideoMetadata {
  return {
    id: "v1",
    source_url: "https://www.tiktok.com/@nasa/video/1",
    title: "A very long clip title that the row layout visually truncates",
    author: "nasa",
    duration_s: 30,
    downloaded: true,
    local_path: null,
    tags: [],
    ...over,
  };
}

afterEach(() => cleanup());

describe("WatchedVideosList (P3.10)", () => {
  it("exposes the full title + subtitle via title attrs (truncated rows are hoverable)", () => {
    const v = video();
    render(<WatchedVideosList videos={[v]} watchEvents={[]} />);

    const link = screen.getByRole("link", { name: videoTitle(v) });
    expect(link).toHaveAttribute("title", videoTitle(v));
    expect(screen.getByText("@nasa")).toHaveAttribute("title", "@nasa");
  });

  it("falls back to the source URL as subtitle + title when there is no author", () => {
    const v = video({ author: null });
    render(<WatchedVideosList videos={[v]} watchEvents={[]} />);

    expect(screen.getByText(v.source_url)).toHaveAttribute(
      "title",
      v.source_url,
    );
  });
});
