# Dashboard UI Polish Design

## Context

Portunus is a single-operator MCP gateway console. The existing Apps and Agent keys workflows are functional, but the interface has inconsistent density, weak hierarchy, generic navigation and controls, visually heavy catalog cards, and limited mobile composition. This pass improves the existing dashboard rather than changing product behavior.

## Decision

Use the **Quiet precision** direction: refine the current light admin system with cool-neutral surfaces, a restrained indigo accent, Geist typography, compact controls, and clear operational status. This follows `PRODUCT.md` and `DESIGN.md`.

Alternatives considered:
- **Dark command center:** stronger novelty, but conflicts with the established light-admin brand and requires a larger token and accessibility redesign.
- **Editorial minimalism:** distinctive, but too sparse for technical metadata and repeated operator tasks.

The visual comparison is stored in `.superpowers/brainstorm/83326-1794326937/content/visual-direction.html` and served by the local brainstorming companion.

## Scope and constraints

- Preserve all React workflows, API contracts, data shapes, and uncommitted product changes.
- Change presentation and small UI-composition details only; no backend work and no new dependencies.
- Cover the shell, Apps catalog, app management drawers, custom MCP form, Agent keys, login, and loading/error/empty states.
- Keep the existing brand icons and semantic status colors.
- Support desktop and mobile widths, keyboard interaction, visible focus, and reduced motion.

## Visual system

Use semantic CSS custom properties for all color, radius, spacing, and shadow decisions. Retain the cool-neutral OKLCH palette and indigo primary. Increase separation between the page canvas, sidebar, raised cards, and recessed areas without introducing heavy shadows. Use tabular numerals for counts and Geist Mono for identifiers and endpoints.

Typography remains compact: 13–14px body text, 12px supporting text, and clear 20–24px page titles. Improve line height, weight contrast, and truncation rather than increasing the overall scale.

## Shell and navigation

The desktop shell uses a fixed-height viewport with a stable sidebar and independently scrolling content. The brand mark becomes a simple geometric gateway symbol instead of a plain letter. Navigation items gain consistent icons, selected-state indicators, and aligned counts. Runner health, API endpoints, and sign-out form a calm utility area.

On narrow screens, the sidebar becomes a compact top region with horizontally accessible primary navigation and a reduced utility row. The page header remains readable and does not hide essential context.

## Apps catalog

The toolbar has one clear row for view tabs, search, and the primary custom-MCP action. Search includes a leading icon and a keyboard-friendly clear affordance when appropriate. Category filters use quieter chips with an unambiguous selected state.

Popular and All apps remain separate sections when no filter is active. Cards use a consistent vertical structure: brand mark, title and status, two-line description, then metadata and tags. Reduce border tinting and decorative hover movement; use a subtle border and elevation response instead. Connected status remains visible without relying on color alone.

Empty, loading, and error states receive intentional containers and useful next actions.

## Drawers and forms

Management drawers gain a structured header, clearer section divisions, sticky close controls, and consistent action alignment. Technical details stay collapsed unless they are the primary content. Tools use readable rows with contained schemas and clear expanded states.

The custom MCP form uses grouped sections for identity, transport, authentication, and routing. Inline styles move to reusable classes. Controls have consistent heights, spacing, helper text, and validation treatment. Existing submission and authorization logic remains unchanged.

## Agent keys and feedback

The keys inventory remains a flush table on desktop, with improved identifier and label hierarchy and quieter action buttons. On narrow screens, rows reflow into labeled cards rather than forcing a wide horizontal table. Newly minted secrets appear in a prominent but restrained notice with explicit copy and dismiss actions.

Errors and success messages use consistent inline notice styles. Destructive actions remain visually distinct.

## Login

The login screen uses the same gateway mark and visual tokens as the console. It remains a centered, compact single-purpose card with clear password focus and error feedback.

## Accessibility and motion

All interactive controls retain semantic elements and accessible names. Focus-visible states use the shared ring token. Status is communicated with text or icons in addition to color. Motion is limited to 120–180ms color, border, shadow, and drawer transitions and is disabled under `prefers-reduced-motion`.

## Verification

- Run `npm run build` in `dashboard`.
- Exercise the real dashboard in a browser at desktop and mobile widths.
- Verify Apps/Connected tabs, search, category filters, custom MCP opening/closing, management drawer rendering, Agent keys rendering, and login layout.
- Check for horizontal overflow, clipping, unreadable contrast, console errors, and regressions to existing controls.
- Review the final diff to ensure no API or backend behavior changed.

## Autonomous-work assumption

The user accepted the visual companion and said they were going to sleep, so this pass proceeds with the recommended direction without further interactive checkpoints. The design and implementation remain available for review and adjustment afterward. The design document is intentionally left uncommitted because committing was not requested and the repository already contains unrelated uncommitted work.
