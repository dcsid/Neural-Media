"use client";

// Thin dynamic-import wrapper so R3F's chunk is split off the initial
// payload and the rest of the Dashboard can stream / paint before the
// 3D pipeline boots.

import dynamic from "next/dynamic";

const BrainMeshSkeleton = () => (
  <div
    aria-hidden
    className="absolute inset-0 bg-[radial-gradient(circle_at_center,rgba(245,165,36,0.05),transparent_60%)]"
  />
);

const BrainMeshLazy = dynamic(
  () =>
    import("@/components/brain/BrainMesh").then((mod) => ({
      default: mod.BrainMesh,
    })),
  { ssr: false, loading: BrainMeshSkeleton },
);

interface BrainMeshSlotProps {
  activation: number;
  className?: string;
}

export function BrainMeshSlot({ activation, className }: BrainMeshSlotProps) {
  return (
    <div
      className={
        "relative aspect-[5/4] w-full overflow-hidden border border-line bg-canvas" +
        (className ? ` ${className}` : "")
      }
    >
      <BrainMeshLazy activation={activation} />
    </div>
  );
}
