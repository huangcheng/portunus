# Design

Portunus aligns to the Janus Dashboard visual system (`~/Projects/janus-dashboard/DESIGN.md`). Same quiet-precision admin language; Portunus-specific chrome only where the product differs (Servers / Keys, no fleet observability).

## Visual Theme

Quiet precision: restrained light admin theme driven by cool-neutral OKLCH tokens and a single indigo accent (hue 258). Page canvas, raised cards, and sidebar use separate surface levels. Accent is reserved for primary actions, focus, and selected state.

## Color

Use the semantic variables in `dashboard/src/style.css` (ported from Janus `spa/src/index.css`). Do not introduce page-specific hex colors. Keep muted text readable; preserve success / warning / destructive.

## Typography

- Geist Variable for interface text; Geist Mono Variable for slugs, URLs, key IDs, endpoints.
- 13–14px body, 12px secondary, `text-sm` controls, `text-xl` sticky page titles.
- Tabular numerals for counts. Long identifiers truncate with `title`.

## Components

- Familiar admin vocabulary: light sidebar, sticky page header, raised cards, flush tables.
- Primary indigo buttons; outline secondary; ghost for quiet actions; destructive for revoke.
- Loading, empty, and error states are intentional — not blank panels.

## Layout

- Sidebar + document-flow content column (Janus `Layout` pattern).
- Nav group label: Operator. Pages: Servers, Keys.
- Sticky header: title + description + actions.
- Content max width ~88rem with card inventory tables.

## Motion

150–250 ms state transitions only. Honor reduced motion. No decorative motion.
