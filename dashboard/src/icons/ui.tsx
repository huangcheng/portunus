import type { ReactNode, SVGProps } from "react";

export type IconName =
  | "apps"
  | "key"
  | "search"
  | "plus"
  | "copy"
  | "check"
  | "chevron-down"
  | "chevron-right"
  | "arrow-up-right"
  | "close"
  | "refresh"
  | "terminal"
  | "logout"
  | "trash"
  | "eye"
  | "shield"
  | "layers"
  | "bolt"
  | "link"
  | "agents";

const paths: Record<IconName, ReactNode> = {
  apps: (
    <>
      <rect x="2.5" y="2.5" width="4" height="4" rx="0.8" />
      <rect x="9.5" y="2.5" width="4" height="4" rx="0.8" />
      <rect x="2.5" y="9.5" width="4" height="4" rx="0.8" />
      <rect x="9.5" y="9.5" width="4" height="4" rx="0.8" />
    </>
  ),
  key: (
    <>
      <circle cx="5.5" cy="8" r="2.5" />
      <path d="M8 8h5.5v2.2M11.2 8v2.2" />
    </>
  ),
  search: (
    <>
      <circle cx="7" cy="7" r="4" />
      <path d="m10 10 3.5 3.5" />
    </>
  ),
  plus: <path d="M8 3v10M3 8h10" />,
  copy: (
    <>
      <rect x="5" y="5" width="8" height="8" rx="1.5" />
      <path d="M10.5 5V3.5A1.5 1.5 0 0 0 9 2H3.5A1.5 1.5 0 0 0 2 3.5V9a1.5 1.5 0 0 0 1.5 1.5H5" />
    </>
  ),
  check: <path d="m3 8.5 3.2 3.2L13 5" />,
  "chevron-down": <path d="m4 6 4 4 4-4" />,
  "chevron-right": <path d="m6 3.5 4.5 4.5L6 12.5" />,
  "arrow-up-right": <path d="M4 12 12 4M5 4h7v7" />,
  close: <path d="m4 4 8 8M12 4l-8 8" />,
  refresh: (
    <>
      <path d="M13 5V2.5L10.8 4.7" />
      <path d="M12.7 5a5.5 5.5 0 1 0 .3 5.5" />
    </>
  ),
  terminal: (
    <>
      <rect x="2" y="3" width="12" height="10" rx="2" />
      <path d="m5 6 2 2-2 2M9 10h2" />
    </>
  ),
  logout: (
    <>
      <path d="M6 3H3.5A1.5 1.5 0 0 0 2 4.5v7A1.5 1.5 0 0 0 3.5 13H6M10 5l3 3-3 3M13 8H6" />
    </>
  ),
  trash: (
    <>
      <path d="M3 4.5h10M6 4.5V3h4v1.5M4.5 4.5l.6 8h5.8l.6-8M6.5 7v3.5M9.5 7v3.5" />
    </>
  ),
  eye: (
    <>
      <path d="M1.8 8s2.2-4 6.2-4 6.2 4 6.2 4-2.2 4-6.2 4S1.8 8 1.8 8Z" />
      <circle cx="8" cy="8" r="1.7" />
    </>
  ),
  shield: (
    <>
      <path d="M8 2 13 4v3.7c0 3.1-2 5.3-5 6.3-3-1-5-3.2-5-6.3V4l5-2Z" />
      <path d="m5.7 8 1.5 1.5 3.2-3.2" />
    </>
  ),
  layers: (
    <>
      <path d="m8 2 6 3-6 3-6-3 6-3Z" />
      <path d="m3 8 5 2.5L13 8M3 11l5 2.5 5-2.5" />
    </>
  ),
  bolt: <path d="m9 2-5 7h4l-1 5 5-7H8l1-5Z" />,
  link: (
    <>
      <path d="m6.5 9.5 3-3M5 11 3.8 12.2a2.3 2.3 0 0 0 3.2 3.2l2-2a2.3 2.3 0 0 0 0-3.2" />
      <path d="m11 5 1.2-1.2a2.3 2.3 0 0 0-3.2-3.2l-2 2a2.3 2.3 0 0 0 0 3.2" transform="translate(1 -1)" />
    </>
  ),
  agents: (
    <>
      <rect x="3" y="4" width="10" height="8" rx="2" />
      <path d="M8 4V2M6 14h4M5.5 8h.01M10.5 8h.01M6 10h4" />
    </>
  ),
};

export function Icon({
  name,
  size = 16,
  ...props
}: { name: IconName; size?: number } & SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 16 16"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.45"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      {paths[name]}
    </svg>
  );
}

export function BrandMark({ className = "" }: { className?: string }) {
  return (
    <span className={`brand-mark ${className}`.trim()} aria-hidden="true">
      <svg viewBox="0 0 32 32" width="27" height="27" fill="none">
        <path d="M7 26V13a9 9 0 0 1 18 0v13" stroke="currentColor" strokeWidth="2" strokeLinecap="square" />
        <path d="M12 26V14a4 4 0 0 1 8 0v12M4 26h24" stroke="currentColor" strokeWidth="2" strokeLinecap="square" />
        <path d="M16 4v5" stroke="currentColor" strokeWidth="2" />
      </svg>
    </span>
  );
}
