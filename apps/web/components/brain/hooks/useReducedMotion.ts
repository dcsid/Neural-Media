"use client";

import { useEffect, useState } from "react";

// Honours the OS "Reduce motion" preference. Per the brief, when this is
// true the mesh stops auto-rotating and animation interpolations
// short-circuit to the target value.
export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);

  return reduced;
}
