"use client";

import { useState } from "react";

// Detects whether the browser can hand us a WebGL context. BrainMesh renders
// a graceful DOM fallback instead of an R3F <Canvas> when this is false —
// without it, a WebGL-less environment (old browser, GPU blocklisted, WebGL
// disabled, some headless/embedded webviews) throws deep inside the Canvas
// mount where the surface error boundary can't catch it.
//
// Resolved synchronously via a lazy initializer: BrainMesh is loaded with
// `ssr: false`, so this only ever runs on the client and there's no null
// "still probing" frame to flash through. A WebGL context can't appear or
// disappear mid-session, so one probe is enough.
export function useWebGLAvailable(): boolean {
  const [available] = useState<boolean>(detectWebGL);
  return available;
}

function detectWebGL(): boolean {
  if (typeof document === "undefined") return false;
  try {
    const canvas = document.createElement("canvas");
    const gl =
      canvas.getContext("webgl2") ??
      canvas.getContext("webgl") ??
      canvas.getContext("experimental-webgl");
    return gl != null;
  } catch {
    // Some browsers throw rather than return null when WebGL is blocked.
    return false;
  }
}
