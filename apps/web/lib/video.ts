// Probe a local video File's duration without keeping the element around. Used
// by the upload page to bound the segment picker at the real file length. Kept
// in its own module so unit tests can mock it — jsdom has no media pipeline, so
// `<video>` never fires `loadedmetadata`. Rejects on a file we can't decode (so
// the UI can ask for a standard MP4).
export function readVideoDuration(file: File): Promise<number> {
  return new Promise((resolve, reject) => {
    if (typeof document === "undefined") {
      reject(new Error("no document"));
      return;
    }
    const url = URL.createObjectURL(file);
    const v = document.createElement("video");
    v.preload = "metadata";
    const done = (fn: () => void) => {
      URL.revokeObjectURL(url);
      fn();
    };
    v.onloadedmetadata = () => {
      const d = Number.isFinite(v.duration) ? v.duration : 0;
      done(() => (d > 0 ? resolve(d) : reject(new Error("zero duration"))));
    };
    v.onerror = () => done(() => reject(new Error("decode error")));
    v.src = url;
  });
}
