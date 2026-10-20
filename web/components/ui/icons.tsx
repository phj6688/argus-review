import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement> & { size?: number };

const stroke = (sw = 1.6) => ({
  fill: "none",
  stroke: "currentColor",
  strokeWidth: sw,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
});

/** The Argus brand "eye" mark. Defaults to amber; `slash` for empty states. */
export function EyeMark({
  size = 26,
  color = "var(--color-accent)",
  strokeWidth = 1.6,
  slash = false,
  ...props
}: IconProps & { color?: string; strokeWidth?: number; slash?: boolean }) {
  return (
    <svg width={size} height={size} viewBox="0 0 26 26" fill="none" {...props}>
      <path d="M2 13C6 6 20 6 24 13C20 20 6 20 2 13Z" stroke={color} strokeWidth={strokeWidth} />
      <circle cx="13" cy="13" r="4.4" stroke={color} strokeWidth={strokeWidth} />
      {slash ? (
        <path d="M4.5 3.5l17 19" stroke={color} strokeWidth={strokeWidth} strokeLinecap="round" />
      ) : (
        <circle cx="13" cy="13" r="1.7" fill={color} />
      )}
    </svg>
  );
}

export const NavOverview = ({ size = 18, ...p }: IconProps) => (
  <svg width={size} height={size} viewBox="0 0 18 18" {...stroke()} {...p}>
    <path d="M2 9.5l7-6 7 6" />
    <path d="M3.5 8v6.5h11V8" />
    <path d="M7.5 14.5v-4h3v4" />
  </svg>
);

export const NavTrigger = ({ size = 18, ...p }: IconProps) => (
  <svg width={size} height={size} viewBox="0 0 18 18" {...stroke()} {...p}>
    <circle cx="9" cy="9" r="6.5" />
    <path d="M7.5 6.2l4 2.8-4 2.8z" />
  </svg>
);

export const NavReviews = ({ size = 18, ...p }: IconProps) => (
  <svg width={size} height={size} viewBox="0 0 18 18" {...stroke()} {...p}>
    <path d="M3 4.5h12M3 9h12M3 13.5h7" />
    <path d="M13 13l1.4 1.4L17 11.5" />
  </svg>
);

export const NavReports = ({ size = 18, ...p }: IconProps) => (
  <svg width={size} height={size} viewBox="0 0 18 18" {...stroke()} {...p}>
    <path d="M3 15V3" />
    <path d="M3 15h12" />
    <rect x="5.5" y="9" width="2.5" height="4" />
    <rect x="10" y="6" width="2.5" height="7" />
  </svg>
);

export const NavBenchmark = ({ size = 18, ...p }: IconProps) => (
  <svg width={size} height={size} viewBox="0 0 18 18" {...stroke()} {...p}>
    <rect x="3" y="6" width="4.5" height="9" rx="1" />
    <rect x="10.5" y="3" width="4.5" height="12" rx="1" />
  </svg>
);

export const NavConfig = ({ size = 18, ...p }: IconProps) => (
  <svg width={size} height={size} viewBox="0 0 18 18" {...stroke()} {...p}>
    <path d="M3 5h6M13 5h2M3 13h2M9 13h6" />
    <circle cx="11" cy="5" r="2" />
    <circle cx="7" cy="13" r="2" />
  </svg>
);

export const NavIntegrations = ({ size = 18, ...p }: IconProps) => (
  <svg width={size} height={size} viewBox="0 0 18 18" {...stroke()} {...p}>
    <path d="M7 11l-2.6 2.6a2.5 2.5 0 01-3.5-3.5L3.5 7.5" />
    <path d="M11 7l2.6-2.6a2.5 2.5 0 013.5 3.5L14.5 10.5" />
    <path d="M6.5 11.5l5-5" />
  </svg>
);

export const SearchIcon = ({ size = 15, ...p }: IconProps) => (
  <svg width={size} height={size} viewBox="0 0 15 15" {...stroke(1.5)} {...p}>
    <circle cx="6.5" cy="6.5" r="4.5" />
    <path d="M10 10l3 3" />
  </svg>
);

export const ChevronLeft = ({ size = 14, ...p }: IconProps) => (
  <svg width={size} height={size} viewBox="0 0 14 14" {...stroke()} {...p}>
    <path d="M8.5 3L4.5 7l4 4" />
  </svg>
);

export const ChevronRight = ({ size = 13, ...p }: IconProps) => (
  <svg width={size} height={size} viewBox="0 0 13 13" {...stroke()} {...p}>
    <path d="M4 2.5l4 4-4 4" />
  </svg>
);

export const CheckIcon = ({ size = 14, ...p }: IconProps) => (
  <svg width={size} height={size} viewBox="0 0 14 14" {...stroke(1.8)} {...p}>
    <path d="M2.5 7.5l3 3 6-7" />
  </svg>
);

export const MinusIcon = ({ size = 14, ...p }: IconProps) => (
  <svg width={size} height={size} viewBox="0 0 14 14" {...stroke()} {...p}>
    <path d="M2 7h10" />
  </svg>
);

export const DownloadIcon = ({ size = 14, ...p }: IconProps) => (
  <svg width={size} height={size} viewBox="0 0 14 14" {...stroke(1.5)} {...p}>
    <path d="M7 1.5v8M4 6.5L7 9.5l3-3" />
    <path d="M2 11.5h10" />
  </svg>
);

export const ApiBrackets = ({ size = 19, ...p }: IconProps) => (
  <svg width={size} height={size} viewBox="0 0 18 18" {...stroke(1.7)} {...p}>
    <path d="M6 3.5L2 9l4 5.5M12 3.5L16 9l-4 5.5" />
  </svg>
);

export const McpGear = ({ size = 19, ...p }: IconProps) => (
  <svg width={size} height={size} viewBox="0 0 18 18" {...stroke()} {...p}>
    <circle cx="9" cy="9" r="2.4" />
    <path d="M9 1.6v2.6M9 13.8v2.6M1.6 9h2.6M13.8 9h2.6M3.7 3.7l1.85 1.85M12.45 12.45l1.85 1.85M14.3 3.7l-1.85 1.85M5.55 12.45L3.7 14.3" />
  </svg>
);

export const InfoCircle = ({ size = 16, ...p }: IconProps) => (
  <svg width={size} height={size} viewBox="0 0 16 16" {...stroke(1.5)} {...p}>
    <circle cx="8" cy="8" r="6.5" />
    <path d="M8 5v3.5M8 11h.01" />
  </svg>
);

export const WarnTriangle = ({ size = 15, ...p }: IconProps) => (
  <svg width={size} height={size} viewBox="0 0 16 16" {...stroke()} {...p}>
    <path d="M8 1.5L15 14H1L8 1.5z" />
    <path d="M8 6.5v3M8 11.5h.01" />
  </svg>
);

export const GitHubMark = ({ size = 20, ...p }: IconProps) => (
  <svg width={size} height={size} viewBox="0 0 16 16" fill="currentColor" {...p}>
    <path d="M8 0C3.6 0 0 3.6 0 8c0 3.5 2.3 6.5 5.5 7.6.4.1.5-.2.5-.4v-1.4c-2.2.5-2.7-1-2.7-1-.4-.9-.9-1.2-.9-1.2-.7-.5.1-.5.1-.5.8.1 1.2.8 1.2.8.7 1.2 1.9.9 2.3.7.1-.5.3-.9.5-1.1-1.8-.2-3.6-.9-3.6-3.9 0-.9.3-1.6.8-2.1-.1-.2-.4-1 .1-2.1 0 0 .7-.2 2.2.8.6-.2 1.3-.3 2-.3s1.4.1 2 .3c1.5-1 2.2-.8 2.2-.8.5 1.1.2 1.9.1 2.1.5.5.8 1.2.8 2.1 0 3-1.8 3.7-3.6 3.9.3.3.6.9.6 1.8v2c0 .2.1.5.6.4C13.7 14.5 16 11.5 16 8c0-4.4-3.6-8-8-8z" />
  </svg>
);
