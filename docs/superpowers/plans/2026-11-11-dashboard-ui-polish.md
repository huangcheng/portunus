# Dashboard UI Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing Portunus dashboard into a cohesive, responsive, production-quality operator console without changing workflows or API behavior.

**Architecture:** Keep the current React component boundaries and API layer. Add a small dependency-free interface icon module, refine component composition where it improves semantics or responsiveness, and consolidate the visual treatment in the existing global CSS file.

**Tech Stack:** React 19, TypeScript, Vite, plain CSS with OKLCH semantic tokens, Geist Variable, ego-browser for end-to-end visual verification.

---

## File structure

- Create `dashboard/src/icons/ui.tsx`: dependency-free interface icons with one shared `Icon` component.
- Modify `dashboard/src/App.tsx`: shell composition, brand, navigation icons, health and utility presentation.
- Modify `dashboard/src/AppsPage.tsx`: search composition, buttons, status labels, drawer semantics, and reusable notice/icon usage.
- Modify `dashboard/src/CreateMcpForm.tsx`: named form-section classes instead of inline layout styles.
- Modify `dashboard/src/KeysPage.tsx`: key identity composition, icon buttons, responsive row labels, and secret notice structure.
- Modify `dashboard/src/Login.tsx`: shared brand mark and login content polish.
- Modify `dashboard/src/style.css`: consolidated tokens, controls, shell, cards, drawers, forms, notices, table-to-card mobile layout, focus and reduced motion.
- Reference `docs/superpowers/specs/2026-11-11-dashboard-ui-polish-design.md` for visual and behavioral constraints.

No dashboard test runner exists. Do not add test scaffolding. Verification uses the existing TypeScript/Vite build and real-browser interaction checks. Do not commit automatically; the repository contains unrelated uncommitted work and no commit was requested.

### Task 1: Establish interface icons and the application shell

**Files:**
- Create: `dashboard/src/icons/ui.tsx`
- Modify: `dashboard/src/App.tsx`
- Modify: `dashboard/src/Login.tsx`
- Modify: `dashboard/src/style.css`

- [ ] **Step 1: Add the shared icon primitive**

Create typed SVG icons with `currentColor`, round caps, a 16×16 viewBox, and accessible hidden decoration:

```tsx
import type { SVGProps } from "react";

export type IconName = "apps" | "key" | "search" | "plus" | "copy" | "check" | "chevron-down" | "chevron-right" | "close" | "refresh" | "external" | "terminal" | "logout" | "trash" | "eye" | "menu";

const paths: Record<IconName, React.ReactNode> = {
  apps: <><rect x="2.5" y="2.5" width="4" height="4" rx="1"/><rect x="9.5" y="2.5" width="4" height="4" rx="1"/><rect x="2.5" y="9.5" width="4" height="4" rx="1"/><rect x="9.5" y="9.5" width="4" height="4" rx="1"/></>,
  key: <><circle cx="5.5" cy="8" r="2.5"/><path d="M8 8h5.5v2.2M11.2 8v2.2"/></>,
  search: <><circle cx="7" cy="7" r="4"/><path d="m10 10 3.5 3.5"/></>,
  plus: <path d="M8 3v10M3 8h10"/>,
  copy: <><rect x="5" y="5" width="8" height="8" rx="1.5"/><path d="M10.5 5V3.5A1.5 1.5 0 0 0 9 2H3.5A1.5 1.5 0 0 0 2 3.5V9a1.5 1.5 0 0 0 1.5 1.5H5"/></>,
  check: <path d="m3 8.5 3.2 3.2L13 5"/>,
  "chevron-down": <path d="m4 6 4 4 4-4"/>,
  "chevron-right": <path d="m6 3.5 4.5 4.5L6 12.5"/>,
  close: <path d="m4 4 8 8M12 4l-8 8"/>,
  refresh: <><path d="M13 5V2.5L10.8 4.7"/><path d="M12.7 5a5.5 5.5 0 1 0 .3 5.5"/></>,
  external: <><path d="M9 3h4v4M13 3 7.5 8.5"/><path d="M11 9.5V12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h2.5"/></>,
  terminal: <><rect x="2" y="3" width="12" height="10" rx="2"/><path d="m5 6 2 2-2 2M9 10h2"/></>,
  logout: <><path d="M6 3H3.5A1.5 1.5 0 0 0 2 4.5v7A1.5 1.5 0 0 0 3.5 13H6M10 5l3 3-3 3M13 8H6"/></>,
  trash: <><path d="M3 4.5h10M6 4.5V3h4v1.5M4.5 4.5l.6 8h5.8l.6-8M6.5 7v3.5M9.5 7v3.5"/></>,
  eye: <><path d="M1.8 8s2.2-4 6.2-4 6.2 4 6.2 4-2.2 4-6.2 4S1.8 8 1.8 8Z"/><circle cx="8" cy="8" r="1.7"/></>,
  menu: <path d="M3 4.5h10M3 8h10M3 11.5h10"/>,
};

export function Icon({ name, size = 16, ...props }: { name: IconName; size?: number } & SVGProps<SVGSVGElement>) {
  return <svg viewBox="0 0 16 16" width={size} height={size} fill="none" stroke="currentColor" strokeWidth="1.45" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...props}>{paths[name]}</svg>;
}

export function BrandMark({ className = "" }: { className?: string }) {
  return <span className={`brand-mark ${className}`.trim()} aria-hidden="true"><svg viewBox="0 0 28 28" width="22" height="22" fill="none"><path d="M6 22V6h8.2a5.8 5.8 0 0 1 0 11.6H9.5" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"/><path d="M18 22h4M20 18v4" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"/></svg></span>;
}
```

Use `import type { ReactNode, SVGProps } from "react"` and `Record<IconName, ReactNode>` if TypeScript configuration does not provide the `React` namespace automatically.

- [ ] **Step 2: Refine shell markup without changing state or data fetching**

Use `BrandMark` in the sidebar and login. Add `Icon name="apps"` and `Icon name="key"` to their respective navigation buttons. Wrap navigation text in `.nav-item-label`, keep `aria-current`, and add `Icon name="terminal"` to API endpoints and `Icon name="logout"` to sign out. Replace text chevrons and close glyphs with shared icons. Update the descriptions to:

```tsx
const description = view === "apps"
  ? "Connect and manage the tools available to your agents."
  : "Create and manage bearer keys for agents calling /mcp.";
```

Do not alter `App` authentication, polling, OAuth message handling, or `Shell` props.

- [ ] **Step 3: Implement shell and responsive styles**

Define a 248px desktop sidebar, 64px brand header, 32px navigation rows, and a sticky 76px page header. Use `.brand-mark` for both sidebar and auth variants. Keep desktop `.app-shell` at `height: 100dvh; overflow: hidden`, make `.inset` scroll, and make the sidebar fixed within that layout. At `max-width: 860px`, restore document scrolling and use a compact top navigation. Do not hide the page description on mobile.

- [ ] **Step 4: Typecheck the shell changes**

Run: `cd dashboard && npm run typecheck`
Expected: exit 0 with no TypeScript errors.

### Task 2: Polish the Apps catalog and management workflows

**Files:**
- Modify: `dashboard/src/AppsPage.tsx`
- Modify: `dashboard/src/CreateMcpForm.tsx`
- Modify: `dashboard/src/style.css`

- [ ] **Step 1: Compose the catalog toolbar and status content**

Wrap the search input in `.search-control` with `Icon name="search"`. Change the custom MCP button to `primary sm` with a plus icon and keep its close behavior. Use shared icons for card chevrons, tool metadata, refresh, and drawer close. Keep tabs and category ARIA roles intact. Add a stable text status in `.app-card-meta`:

```tsx
<span className={`app-meta-item ${active ? "is-connected" : "muted"}`}>
  {active ? "Connected" : configured ? "Needs authorization" : "Not connected"}
</span>
{label && <span className="app-meta-item"><Icon name="terminal" size={12} />{label}</span>}
```

- [ ] **Step 2: Improve drawer semantics and notices**

Keep all provider logic unchanged. Add `aria-modal="true"` to the drawer and `aria-labelledby` pointing to a stable drawer heading id. Replace the inline heading styles in `ServerToolsPanel` with classes. Wrap success/error output in `.notice.success` and `.notice.error` containers while preserving the existing messages. Keep tools, schemas, lifecycle controls, and endpoint details functional.

- [ ] **Step 3: Replace form inline styles with reusable section classes**

In `CreateMcpForm`, use `.form-section`, `.form-section-heading`, `.form-grid`, `.field.command-field`, and `.form-actions` for the existing fields. Preserve field order and submission behavior. Example composition:

```tsx
<section className="form-section" aria-labelledby="custom-mcp-identity">
  <div className="form-section-heading">
    <h3 id="custom-mcp-identity">Connection</h3>
    <p>Identify the MCP and choose its transport.</p>
  </div>
  {/* existing transport, name, slug, command, args and URL controls */}
</section>
```

Use equivalent named sections for Authentication and Routing. Do not add placeholder controls or new form state.

- [ ] **Step 4: Implement catalog, card, drawer, and form CSS**

Use a three-column catalog grid above 1280px, two columns at medium widths, and one below 680px. Cards must have a consistent minimum height, 16px padding, 10px radius, quiet borders, and no translate-on-hover. Tags are secondary metadata, not competing pills. Drawers are `min(500px, 100%)` with a sticky header and 20–24px content spacing. Forms use 12px vertical rhythm and full-width mobile actions.

- [ ] **Step 5: Build the Apps changes**

Run: `cd dashboard && npm run build`
Expected: TypeScript and Vite complete successfully; `dist/index.html` is regenerated.

### Task 3: Polish Agent keys, secret feedback, and login

**Files:**
- Modify: `dashboard/src/KeysPage.tsx`
- Modify: `dashboard/src/Login.tsx`
- Modify: `dashboard/src/style.css`

- [ ] **Step 1: Refine key inventory markup**

Give the prefix cell a `.key-identity` wrapper with a key icon and readable truncated identifier. Use sentence-case `All apps` access text and `Active` state. Add `data-label` attributes to cells for mobile rendering:

```tsx
<td data-label="Key"><div className="key-identity"><Icon name="key" size={14} /><code>{k.key_id.slice(0, 8)}…</code></div></td>
<td data-label="Label">{k.label?.trim() ? k.label : <span className="muted">Untitled key</span>}</td>
<td data-label="Access"><span className="access-pill">All apps</span></td>
<td data-label="State"><span className="status on">Active</span></td>
```

Use shared copy/trash icons while retaining the full button labels on desktop. Preserve reveal, copy, delete, confirmation, and mint behavior.

- [ ] **Step 2: Structure secret and inline notices**

Use a `.secret-banner-header` with an eye icon and a `.secret-actions` row. Keep the recoverable-secret explanation and token visible. Render page errors as `.notice.error` and minted secrets as `.secret-banner` without altering token contents or dismissal behavior.

- [ ] **Step 3: Refine login composition**

Use `BrandMark`, a small `Operator console` eyebrow, `Welcome back` as the title, and the existing password form. Preserve authentication behavior and error messages.

- [ ] **Step 4: Implement responsive keys and feedback styles**

At `max-width: 680px`, hide the table head and render each `tr` as a bordered card. Render `td` as flex rows with `td::before { content: attr(data-label); }`; make the actions cell full width and keep buttons at least 36px high. On desktop retain the flush table. Style success, error, and warning notices with semantic backgrounds and readable foregrounds.

- [ ] **Step 5: Typecheck the completed UI**

Run: `cd dashboard && npm run typecheck`
Expected: exit 0 with no TypeScript errors.

### Task 4: Verify the real interface and fix visual regressions

**Files:**
- Modify only if verification exposes issues: `dashboard/src/App.tsx`, `dashboard/src/AppsPage.tsx`, `dashboard/src/KeysPage.tsx`, `dashboard/src/CreateMcpForm.tsx`, `dashboard/src/Login.tsx`, `dashboard/src/style.css`

- [ ] **Step 1: Start the dashboard through its real Vite entry point**

Run: `cd dashboard && npm run dev -- --host 127.0.0.1`
Expected: Vite prints a local URL and remains running. Use the configured gateway proxy/backend if available; do not bypass authentication or write test data to production services.

- [ ] **Step 2: Capture desktop and mobile layouts in ego-browser**

Use one TaskSpace. Open the Vite URL at 1440×1000, capture the login or authenticated Apps screen, then resize to 390×844. Inspect full-page screenshots and DOM measurements:

```js
const overflow = await page.evaluate(() => ({
  viewport: document.documentElement.clientWidth,
  document: document.documentElement.scrollWidth,
  title: document.title,
}));
console.log(overflow);
```

Expected: `document <= viewport`, no clipped controls, readable page title and navigation.

- [ ] **Step 3: Exercise core UI paths**

With existing authenticated state, click Apps and Agent keys, All and Connected, type and clear a search, select a category, open and close Add custom MCP, open an app drawer and close it with Escape and the close button, and open API endpoints. Do not submit credentials, mint/delete keys, probe servers, or authorize OAuth during visual verification.

Expected: all existing interactions still work; focus remains visible; no browser console errors.

- [ ] **Step 4: Run the final production build**

Run: `cd dashboard && npm run build`
Expected: exit 0, successful TypeScript check and Vite production build.

- [ ] **Step 5: Inspect the final diff and workspace**

Run: `git diff --check && git status --short`
Expected: no whitespace errors. Only intended dashboard files and the design/plan documents are newly changed by this pass; pre-existing modifications remain intact.
