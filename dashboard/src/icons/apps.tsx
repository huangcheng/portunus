import type { CSSProperties } from "react";
import { appInitial, appInitialHue } from "../appInitial";

export type AppIconId =
  | "box"
  | "dropbox"
  | "nutstore"
  | "koofr"
  | "onedrive"
  | "gdrive"
  | "yandex"
  | "drime"
  | "mega"
  | "flomo"
  | "dida365"
  | "ardot"
  | "weread"
  | "guangyapan"
  | "weiyun"
  | "raindrop"
  | "cubox"
  | "github"
  | "cloudflare"
  | "aliyun"
  | "vultr"
  | "vercel"
  | "tencentcloud"
  | "flyai"
  | "youdaonote"
  | "ima"
  | "weknora"
  | "pikpak"
  | "qqmusic"
  | "generic";

export function appIconIdForSlug(slug: string | undefined | null): AppIconId {
  if (!slug) return "generic";
  if (slug === "box") return "box";
  if (slug === "dropbox") return "dropbox";
  if (slug === "nutstore") return "nutstore";
  if (slug === "koofr") return "koofr";
  if (slug === "onedrive") return "onedrive";
  if (slug === "gdrive" || slug === "google-drive" || slug === "googledrive") return "gdrive";
  if (slug === "yandex") return "yandex";
  if (slug === "drime") return "drime";
  if (slug === "mega") return "mega";
  if (slug === "flomo") return "flomo";
  if (slug === "dida365" || slug === "ticktick") return "dida365";
  if (slug === "ardot") return "ardot";
  if (slug === "weread") return "weread";
  if (slug === "guangyapan") return "guangyapan";
  if (slug === "weiyun") return "weiyun";
  if (slug === "raindrop") return "raindrop";
  if (slug === "cubox") return "cubox";
  if (slug === "github") return "github";
  if (slug === "cloudflare") return "cloudflare";
  if (slug === "aliyun" || slug === "alibaba" || slug === "alibabacloud") return "aliyun";
  if (slug === "vultr") return "vultr";
  if (slug === "vercel") return "vercel";
  if (slug === "tencentcloud" || slug === "cloudbase" || slug === "tcb") return "tencentcloud";
  if (slug === "flyai" || slug === "fliggy") return "flyai";
  if (slug === "youdaonote" || slug === "youdao") return "youdaonote";
  if (slug === "ima") return "ima";
  if (slug === "weknora") return "weknora";
  if (slug === "pikpak") return "pikpak";
  if (slug === "qqmusic" || slug === "qq-music") return "qqmusic";
  return "generic";
}

const OFFICIAL_ICON_FILES: Partial<Record<AppIconId, string>> = {
  ardot: "ardot.png",
  box: "box.svg",
  dropbox: "dropbox.svg",
  nutstore: "nutstore.png",
  koofr: "koofr.png",
  onedrive: "onedrive.svg",
  gdrive: "gdrive.png",
  yandex: "yandex.png",
  drime: "drime.png",
  mega: "mega.svg",
  flomo: "flomo.png",
  dida365: "dida365.png",
  weread: "weread.png",
  guangyapan: "guangyapan.svg",
  weiyun: "weiyun.png",
  raindrop: "raindrop.svg",
  cubox: "cubox.png",
  github: "github.svg",
  cloudflare: "cloudflare.svg",
  aliyun: "aliyun.svg",
  vultr: "vultr.svg",
  vercel: "vercel.svg",
  // tencentcloud / flyai: no redistributable mark sourced yet — AppBrandMark falls back to initials.
  youdaonote: "youdaonote.png",
  ima: "ima.svg",
  weknora: "weknora.png",
  pikpak: "pikpak.png",
  qqmusic: "qqmusic.png",
};

export function AppBrandMark({
  id,
  label,
  className,
}: {
  /** Catalog/server slug (or known AppIconId); resolved via appIconIdForSlug. */
  id: string;
  /** Display name for the initial-letter fallback (Latin or CJK). */
  label?: string;
  className?: string;
}) {
  // appIconIdForSlug already maps every known slug (incl. the ticktick alias)
  // to its icon id and returns "generic" otherwise — identical to the old chain.
  const kind: AppIconId = appIconIdForSlug(id);
  const file = OFFICIAL_ICON_FILES[kind];
  const hue = appInitialHue(id.trim() || label);
  const initial = appInitial(label?.trim() || id);
  return (
    <div
      className={`app-card-mark ${kind}${file ? " is-official" : " is-initial"}${className ? ` ${className}` : ""}`}
      style={file ? undefined : ({ "--app-initial-hue": String(hue) } as CSSProperties)}
      aria-hidden
    >
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
        <span className="app-initial">{initial}</span>
      )}
    </div>
  );
}
