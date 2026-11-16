# Design

Portunus uses an architectural control-room visual system for its single-operator MCP gateway console. The interface should feel precise, assured, and distinctive without imitating a multi-tenant analytics or decorative SaaS dashboard. The detailed direction is recorded in `docs/superpowers/specs/2026-11-11-dashboard-visual-redesign-design.md` and persistent design context in `.impeccable.md`.

## Visual Theme

Use deep-ink structural framing, a light cool-gray workspace, near-white working surfaces, chartreuse primary accents, and strong editorial typography. Dark navigation, operational overview panels, featured apps, and drawer identity headers form one coherent architectural frame around a readable light workspace. Avoid generic minimal-admin styling, uniform white card grids, purple-blue gradients, glassmorphism, fake telemetry, and decorative analytics.

## Color and Contrast

Use the semantic variables in `dashboard/src/style.css` as the source of truth. Keep one chartreuse accent for primary actions, current navigation, focus, and the gateway motif. Use tinted neutrals rather than pure black or white. Preserve readable success, warning, and destructive states, and never communicate status through color alone.

The main workspace uses a quiet cool-gray background and near-white cards. Do not place large color gradients, grids, or other decorative textures behind reading content. Geometric grids belong in dark structural or illustrative regions where they cannot interfere with copy.

Essential text must remain clearly readable on both light and dark surfaces. Target WCAG AA contrast: 4.5:1 for normal text and 3:1 for large text. Supporting copy on dark panels uses substantially opaque foreground colors rather than faint gray; reserve low-opacity text for nonessential coordinates and decorative labels.

## Typography

- Geist Variable for interface text; Geist Mono Variable for slugs, URLs, key IDs, technical labels, and counts.
- Fluid 36–48px page titles, 23–25px major-section titles, 13–14px body text, and 12–13px supporting copy.
- Strong weight and scale contrast rather than uniformly small type. Use uppercase mono labels sparingly.
- Tabular numerals for counts. Long identifiers truncate with `title`; paragraph copy stays within readable line lengths.

## Iconography

Use official service brand assets stored locally in `dashboard/public/icons/apps/` and render them through `AppBrandMark` in `dashboard/src/icons/apps.tsx`. Do not substitute generated approximations for known service logos. Preserve each asset's aspect ratio and transparent background; use a neutral backing only when needed for legibility. Unknown or custom MCP services use an initial-letter mark (first Latin or CJK grapheme of the display name on a stable slug/label-hashed hue) rather than inventing a logo.

## Components and Layout

- Dark desktop sidebar with geometric brand mark, clear selected navigation, and framed gateway status.
- Compact utility breadcrumb plus a large in-content page introduction; avoid duplicate page headers.
- Apps uses a data-driven dark gateway overview, a distinct Popular composition, then the complete working catalog. Show only values available from existing APIs.
- Popular uses a horizontal card rail with chartreuse emphasis on the first card and deep-ink companion cards. The complete catalog uses near-white working cards with official brand marks (or initial-letter marks when no asset exists) and clearer supporting copy.
- Use asymmetric or varied compositions instead of repeating identical cards and section treatments. Controls remain familiar: primary, outline, ghost, and destructive.
- Drawers have dark identity headers and readable light working content. Forms use numbered sections and consistent 40–44px controls.
- Keys uses a substantial inventory table on desktop and labeled cards on narrow screens, with intentional loading and empty states.
- Login uses a split brand/authentication composition that collapses cleanly on mobile.
- Content has a responsive max-width and must fit a 390px viewport without horizontal scrolling.

## Motion and Accessibility

Use 160–240ms opacity, transform, color, border, and shadow transitions with natural deceleration. Motion communicates state and hierarchy; no decorative continuous animation. Honor reduced motion. Preserve semantic controls, visible focus, keyboard interaction, readable contrast, and reduced-motion support across every screen.
