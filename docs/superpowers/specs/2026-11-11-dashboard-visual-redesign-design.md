# Dashboard Visual Redesign Design

## Context

The first polish pass made the dashboard consistent but did not give it enough identity. The user rejected the result as cheap, ugly, and too minimal. The main problems are uniform white surfaces, repeated cards with identical visual weight, small typography, weak brand expression, and large unused areas. This pass is a visual redesign, not another spacing refinement.

## Decision

Use **Architectural control room**: a deep-ink navigation frame and gateway overview, warm porcelain workspace surfaces, vivid chartreuse accents, strong editorial typography, and layered technical details. The look should feel designed and distinctive while remaining a compact operator tool.

Alternatives considered:
- **Full dark mission control:** immersive and dramatic, but dense forms, long URLs, and repeated management tasks would be less readable.
- **Editorial color blocking:** memorable typography and asymmetric color fields, but the layout would be less efficient for a repeatable app inventory.
- **Architectural control room (selected):** preserves a readable light working area while using dark framing, an operational diagram, and varied section compositions to create identity.

The user’s standing autonomous-work instruction and the current auto permission mode allow this direction to proceed without another approval round.

## Scope and constraints

- Preserve every existing React workflow, API contract, data shape, and uncommitted product change.
- Change presentation and UI composition only; no backend work and no new dependencies.
- Cover the shell, Apps overview and catalog, drawers, custom MCP form, Agent keys, login, and feedback states.
- Use only real values already available from the APIs. Do not invent usage charts, activity, timestamps, or health data.
- Keep existing app brand icons and semantic success, warning, and destructive colors.
- Support desktop and mobile, keyboard interaction, visible focus, and reduced motion.

## Visual system

Use deep navy-black for the sidebar and operational hero, warm off-white for the page canvas, white for working surfaces, and chartreuse for primary actions and active accents. Muted lavender-gray supports secondary text on dark surfaces. Borders remain visible but not every region is boxed. Soft, broad shadows and inset highlights establish depth without glassmorphism.

Geist Variable remains the interface font, with much stronger scale contrast: 36–44px page titles, 15–18px section titles, and readable 13–14px body copy. Uppercase mono labels are used sparingly for technical context. Geist Mono remains reserved for identifiers, endpoints, counts, and operational labels.

Corner radii increase on major compositions and decrease on controls. The page uses deliberate asymmetry: a large overview composition, a featured popular strip, then the complete working catalog.

## Shell and navigation

The desktop sidebar is a dark, fixed-width architectural frame with a luminous brand mark, large product wordmark, concise product descriptor, and clear selected navigation states. A framed gateway-status module anchors the bottom, with API endpoints and sign-out grouped as utilities.

The content header becomes a compact utility breadcrumb rather than a second page title. The main page introduction lives in the content and has enough scale to establish hierarchy. On mobile, navigation condenses into a dark top region without hiding primary actions or causing horizontal overflow.

## Apps overview and catalog

A dark overview panel introduces the Apps page with a direct headline, real catalog/connection/tool counts, and a dependency-free gateway map showing Apps → Portunus → Agents. The map is contextual artwork built from CSS and inline SVG, not fake telemetry.

The catalog toolbar combines view tabs, a prominent search field, and the custom MCP action. Category filters read as a separate navigation row. Popular apps appear in a distinct featured composition with larger cards and a dark/lime treatment. All apps use brighter working cards with stronger brand marks, clearer connection state, fewer repetitive tags, and a deliberate hover response. Existing Popular and All apps data behavior remains unchanged.

## Agent keys

The Keys page gets a structured introduction with the real key count and a concise security explanation. The inventory becomes a substantial working surface with a clear header, readable identifiers, labeled access/state, and polished actions. Empty and loading states use a key illustration and direct next step instead of a blank white panel. Newly minted secrets use a high-contrast dark notice with a visible copy action.

## Drawers, forms, and login

Management drawers retain all provider logic but use a dark identity header, clearer content sections, stronger primary actions, and readable technical disclosures. Custom MCP creation uses numbered sections and a more spacious form grid. Controls have consistent 40–44px heights and unambiguous focus states.

Login becomes a split composition: a dark brand/gateway panel and a focused authentication card on the warm workspace canvas. On mobile, the brand panel compresses above the form.

## Accessibility and motion

Semantic controls, labels, ARIA state, and keyboard behavior remain intact. Text contrast is checked on both dark and light surfaces. Focus uses a visible chartreuse/indigo ring appropriate to the surface. Motion is limited to 160–240ms opacity, color, border, shadow, and drawer transitions and is disabled under `prefers-reduced-motion`.

## Verification

- Run the existing TypeScript and Vite build in `dashboard`.
- Exercise the real dashboard in a browser at desktop and mobile widths.
- Verify Apps/Connected tabs, search, category filters, custom MCP opening/closing, management drawers, API endpoints, key creation/copy/cancel, and login layout.
- Inspect populated and empty states, long content, horizontal overflow, console errors, and keyboard focus.
- Review the final diff to ensure no API or backend behavior changed and preserve unrelated working-tree changes.

## Documentation and delivery

Update `DESIGN.md` to describe the new approved visual system so repository guidance does not continue to prescribe the rejected direction. Leave the work uncommitted because no commit was requested and the repository contains unrelated uncommitted work.
