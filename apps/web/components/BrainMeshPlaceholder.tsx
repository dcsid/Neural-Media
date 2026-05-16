import clsx from "clsx";

interface BrainMeshPlaceholderProps {
  className?: string;
  label?: string;
}

// Rendered until the brain-viz worker ships
// `apps/web/components/brain/BrainMesh.tsx`. Once that component lands, the
// pages that consume the hero slot can import it instead.
export function BrainMeshPlaceholder({
  className,
  label = "placeholder — cortical mesh",
}: BrainMeshPlaceholderProps) {
  return (
    <div
      className={clsx(
        "relative flex items-center justify-center border border-line bg-canvas",
        "aspect-[5/4] w-full overflow-hidden",
        className,
      )}
      aria-label="Cortical mesh placeholder"
    >
      <div className="absolute inset-0 bg-[radial-gradient(circle_at_center,rgba(245,165,36,0.05),transparent_60%)]" />
      <p className="eyebrow relative z-10">{label}</p>
    </div>
  );
}
