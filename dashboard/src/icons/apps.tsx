export function GenericMcpGlyph({ size = 22 }: { size?: number }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden>
      <rect x="3.5" y="7" width="8" height="10" rx="1.5" />
      <rect x="12.5" y="7" width="8" height="10" rx="1.5" />
      <path d="M11.5 10.5h1M11.5 13.5h1" strokeLinecap="round" />
      <path d="M7.5 4.5v2.5M7.5 17v2.5M16.5 4.5v2.5M16.5 17v2.5" strokeLinecap="round" />
    </svg>
  );
}

export type AppIconId =
  | "box"
  | "dropbox"
  | "nutstore"
  | "koofr"
  | "onedrive"
  | "flomo"
  | "dida365"
  | "ardot"
  | "weread"
  | "guangyapan"
  | "weiyun"
  | "pikpak"
  | "generic";

export function appIconIdForSlug(slug: string | undefined | null): AppIconId {
  if (!slug) return "generic";
  if (slug === "box") return "box";
  if (slug === "dropbox") return "dropbox";
  if (slug === "nutstore") return "nutstore";
  if (slug === "koofr") return "koofr";
  if (slug === "onedrive") return "onedrive";
  if (slug === "flomo") return "flomo";
  if (slug === "dida365" || slug === "ticktick") return "dida365";
  if (slug === "ardot") return "ardot";
  if (slug === "weread") return "weread";
  if (slug === "guangyapan") return "guangyapan";
  if (slug === "weiyun") return "weiyun";
  if (slug === "pikpak") return "pikpak";
  return "generic";
}

const OFFICIAL_ICON_FILES: Partial<Record<AppIconId, string>> = {
  ardot: "ardot.png",
  box: "box.svg",
  dropbox: "dropbox.svg",
  nutstore: "nutstore.png",
  koofr: "koofr.png",
  onedrive: "onedrive.svg",
  flomo: "flomo.png",
  dida365: "dida365.png",
  weread: "weread.png",
  guangyapan: "guangyapan.svg",
  weiyun: "weiyun.png",
  pikpak: "pikpak.png",
};

export function AppBrandMark({
  id,
  className,
}: {
  id: AppIconId | string;
  className?: string;
}) {
  // appIconIdForSlug already maps every known slug (incl. the ticktick alias)
  // to its icon id and returns "generic" otherwise — identical to the old chain.
  const kind: AppIconId = appIconIdForSlug(id);
  const file = OFFICIAL_ICON_FILES[kind];
  return (
    <div className={`app-card-mark ${kind}${file ? " is-official" : ""}${className ? ` ${className}` : ""}`} aria-hidden>
      {file ? (
        <img
          className={`app-brand-image ${kind}`}
          src={`/icons/apps/${file}`}
          width={48}
          height={48}
          alt=""
          draggable={false}
        />
      ) : (
        <GenericMcpGlyph />
      )}
    </div>
  );
}
