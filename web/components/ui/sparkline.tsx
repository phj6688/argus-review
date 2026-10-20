/* A tiny normalized sparkline: SVG polyline + end-cap dot. */
export function Sparkline({
  data,
  color,
  height = 42,
}: {
  data: number[];
  color: string;
  height?: number;
}) {
  const W = 240;
  const pad = 3;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const span = max - min || 1;
  const n = data.length;
  const pts = data.map((v, i) => {
    const x = n === 1 ? W : (i / (n - 1)) * W;
    const y = pad + (1 - (v - min) / span) * (height - pad * 2);
    return [x, y] as const;
  });
  const last = pts[pts.length - 1];
  return (
    <svg
      width="100%"
      height={height}
      viewBox={`0 0 ${W} ${height}`}
      preserveAspectRatio="none"
      style={{ flex: 1 }}
    >
      <polyline
        points={pts.map((p) => `${p[0]},${p[1]}`).join(" ")}
        fill="none"
        stroke={color}
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
        vectorEffect="non-scaling-stroke"
      />
      <circle cx={last[0]} cy={last[1]} r={3} fill={color} />
    </svg>
  );
}
