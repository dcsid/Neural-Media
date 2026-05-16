interface SparklineProps {
  values: number[];
  width?: number;
  height?: number;
  domainMax?: number;
  className?: string;
  ariaLabel?: string;
}

// Pure SVG sparkline. No client-side runtime. Rendered for region timeseries
// on the Detail view and the Compare view.
export function Sparkline({
  values,
  width = 220,
  height = 28,
  domainMax,
  className,
  ariaLabel,
}: SparklineProps) {
  if (values.length === 0) {
    return (
      <svg
        width={width}
        height={height}
        className={className}
        aria-label={ariaLabel}
      >
        <line
          x1={0}
          y1={height / 2}
          x2={width}
          y2={height / 2}
          stroke="currentColor"
          strokeOpacity={0.2}
          strokeWidth={1}
        />
      </svg>
    );
  }

  const max = domainMax ?? Math.max(1e-6, ...values);
  const min = Math.min(0, ...values);
  const range = Math.max(1e-6, max - min);
  const step = values.length > 1 ? width / (values.length - 1) : width;

  const path = values
    .map((v, i) => {
      const x = i * step;
      const y = height - ((v - min) / range) * height;
      return `${i === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");

  return (
    <svg
      width={width}
      height={height}
      className={className}
      role="img"
      aria-label={ariaLabel}
      preserveAspectRatio="none"
      viewBox={`0 0 ${width} ${height}`}
    >
      <path
        d={path}
        fill="none"
        stroke="currentColor"
        strokeWidth={1.25}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}
