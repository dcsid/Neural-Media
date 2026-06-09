import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render } from "@testing-library/react";

import { GalleryVideo } from "@/components/gallery/GalleryVideo";

// jsdom has no media stack: play()/pause() are unimplemented and there is no
// real decode/timeupdate loop. We stub the element so the controlled wiring
// (play/pause, seek, time reporting, error) can be exercised deterministically.

afterEach(() => cleanup());

let playSpy: ReturnType<typeof vi.spyOn>;
let pauseSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  playSpy = vi
    .spyOn(HTMLMediaElement.prototype, "play")
    .mockImplementation(() => Promise.resolve());
  pauseSpy = vi
    .spyOn(HTMLMediaElement.prototype, "pause")
    .mockImplementation(() => undefined);
});

/** Grab the rendered <video> and make duration/currentTime deterministic
 *  (jsdom's are read-only / stubbed). */
function getVideo(container: HTMLElement): HTMLVideoElement {
  const v = container.querySelector("video");
  if (!v) throw new Error("expected a <video> element");
  Object.defineProperty(v, "duration", { value: 42, configurable: true });
  Object.defineProperty(v, "currentTime", { value: 0, writable: true, configurable: true });
  return v;
}

const fire = (v: HTMLVideoElement, type: string) => fireEvent(v, new Event(type));

describe("GalleryVideo", () => {
  it("play/pause follows the `playing` prop (only once ready)", () => {
    const { container, rerender } = render(
      <GalleryVideo src="/demo-clips/x.mp4" playing={false} />,
    );
    const v = getVideo(container);
    fire(v, "loadedmetadata"); // → ready
    expect(playSpy).not.toHaveBeenCalled();

    rerender(<GalleryVideo src="/demo-clips/x.mp4" playing={true} />);
    expect(playSpy).toHaveBeenCalledTimes(1);

    rerender(<GalleryVideo src="/demo-clips/x.mp4" playing={false} />);
    expect(pauseSpy).toHaveBeenCalled();
  });

  it("seeks to seekRequest.sec when the nonce changes (re-fires same sec)", () => {
    const { container, rerender } = render(
      <GalleryVideo src="/x.mp4" playing={false} seekRequest={{ sec: 5, nonce: 1 }} />,
    );
    const v = getVideo(container);
    fire(v, "loadedmetadata"); // flushes the seek requested before metadata
    expect(v.currentTime).toBe(5);

    rerender(<GalleryVideo src="/x.mp4" playing={false} seekRequest={{ sec: 12, nonce: 2 }} />);
    expect(v.currentTime).toBe(12);

    // Same sec, new nonce → must re-seek.
    v.currentTime = 0;
    rerender(<GalleryVideo src="/x.mp4" playing={false} seekRequest={{ sec: 12, nonce: 3 }} />);
    expect(v.currentTime).toBe(12);
  });

  it("reports playback time via timeupdate → onTime", () => {
    const onTime = vi.fn();
    const { container } = render(
      <GalleryVideo src="/x.mp4" playing={false} onTime={onTime} />,
    );
    const v = getVideo(container);
    fire(v, "loadedmetadata");
    v.currentTime = 3.5;
    fire(v, "timeupdate");
    expect(onTime).toHaveBeenCalledWith(3.5);
  });

  it("calls onLoaded(duration) on loadedmetadata", () => {
    const onLoaded = vi.fn();
    const { container } = render(
      <GalleryVideo src="/x.mp4" playing={false} onLoaded={onLoaded} />,
    );
    const v = getVideo(container);
    fire(v, "loadedmetadata");
    expect(onLoaded).toHaveBeenCalledWith(42);
  });

  it("calls onEnded when playback ends", () => {
    const onEnded = vi.fn();
    const { container } = render(
      <GalleryVideo src="/x.mp4" playing={false} onEnded={onEnded} />,
    );
    const v = getVideo(container);
    fire(v, "loadedmetadata");
    fire(v, "ended");
    expect(onEnded).toHaveBeenCalledTimes(1);
  });

  it("degrades to an honest error state when the source errors (no crash)", () => {
    const { container, getByRole } = render(
      <GalleryVideo src="/missing.mp4" playing={false} />,
    );
    const v = getVideo(container);
    fire(v, "error");
    expect(getByRole("status")).toHaveTextContent(/unavailable/i);
  });

  it("swallows a play() AbortError without throwing", async () => {
    playSpy.mockImplementationOnce(() =>
      Promise.reject(Object.assign(new Error("interrupted"), { name: "AbortError" })),
    );
    const { container, rerender } = render(
      <GalleryVideo src="/x.mp4" playing={false} />,
    );
    const v = getVideo(container);
    fire(v, "loadedmetadata");
    rerender(<GalleryVideo src="/x.mp4" playing={true} />);
    await Promise.resolve(); // let the rejected play() settle
    expect(playSpy).toHaveBeenCalled(); // reached play(); rejection was swallowed
  });

  it("defaults to muted and exposes a working unmute control", () => {
    const { container, getByLabelText } = render(
      <GalleryVideo src="/x.mp4" playing={false} />,
    );
    const v = getVideo(container);
    fire(v, "loadedmetadata");
    expect(v.muted).toBe(true);

    fireEvent.click(getByLabelText(/unmute video/i));
    expect(v.muted).toBe(false);
  });
});
