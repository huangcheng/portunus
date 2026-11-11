# Dashboard Visual Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the rejected minimal admin styling with a distinctive, rich architectural control-room interface while preserving every dashboard workflow.

**Architecture:** Keep the existing React components and API layer. Add small presentational components inside the current page files, derive all summary values from existing state, and rebuild the existing global CSS around new semantic tokens. No new dependencies or backend changes.

**Tech Stack:** React 19, TypeScript, Vite, plain CSS, Geist Variable, inline SVG, ego-browser for end-to-end verification.

**Reference:** `docs/superpowers/specs/2026-11-11-dashboard-visual-redesign-design.md`.

No dashboard test runner exists, so do not add test scaffolding. Verify with the existing TypeScript/Vite build and real browser interactions. Do not commit automatically; committing was not requested and the tree contains unrelated work.

---

### Task 1: Rebuild the shell and visual tokens

**Files:**
- Modify: `dashboard/src/App.tsx`
- Modify: `dashboard/src/icons/ui.tsx`
- Modify: `dashboard/src/style.css`
- Modify: `DESIGN.md`

- [ ] **Step 1: Replace the small header with utility context and a full page introduction**

Keep `Shell` state and props unchanged. Replace its header markup with this composition:

```tsx
<div className="inset">
  <header className="utility-bar">
    <div className="breadcrumb"><span>Portunus</span><Icon name="chevron-right" size={12} /><strong>{title}</strong></div>
    <div className="environment-badge"><span className="environment-dot" />Private gateway</div>
  </header>
  <main className="page-body" id="main-content" tabIndex={-1}>
    <div className={`page-intro ${view}`}>
      <div>
        <p className="page-eyebrow">{view === "apps" ? "Integration directory" : "Access control"}</p>
        <h1>{view === "apps" ? "Your toolstack, connected." : "Keys to your gateway."}</h1>
        <p className="lede">{description}</p>
      </div>
      <div className="page-intro-index" aria-hidden="true">{view === "apps" ? "01" : "02"}</div>
    </div>
    {children}
  </main>
</div>
```

Update the descriptions to “One control plane for every tool your agents use.” and “Create, distribute, and revoke agent credentials from one secure inventory.” Change the sidebar label to “Workspace”, add a small “MCP CONTROL PLANE” brand descriptor, and wrap runner health in `.gateway-status` with separate “Gateway status” and live-runner rows.

- [ ] **Step 2: Extend the icon primitive without adding a library**

Add these names to `IconName`: `arrow-up-right`, `shield`, `layers`, `bolt`, `link`, `agents`. Supply simple 16×16 paths using the existing `currentColor` SVG pattern. Replace the brand glyph with a geometric open-gateway arch and threshold, retaining the `BrandMark` API.

- [ ] **Step 3: Establish the architectural control-room tokens**

At the top of `style.css`, define and then use this palette throughout the stylesheet:

```css
:root {
  --background: oklch(0.965 0.008 95);
  --foreground: oklch(0.235 0.025 265);
  --card: oklch(0.995 0.003 95);
  --card-foreground: var(--foreground);
  --primary: oklch(0.88 0.19 120);
  --primary-hover: oklch(0.82 0.18 120);
  --primary-foreground: oklch(0.22 0.035 265);
  --secondary: oklch(0.925 0.012 265);
  --secondary-foreground: oklch(0.35 0.025 265);
  --muted: oklch(0.94 0.009 95);
  --muted-foreground: oklch(0.5 0.02 265);
  --accent: oklch(0.91 0.035 265);
  --accent-foreground: oklch(0.3 0.04 265);
  --border: oklch(0.865 0.015 265);
  --input: oklch(0.82 0.018 265);
  --ring: oklch(0.65 0.16 265);
  --sidebar: oklch(0.205 0.025 265);
  --sidebar-foreground: oklch(0.94 0.01 265);
  --sidebar-accent: oklch(0.28 0.035 265);
  --sidebar-accent-foreground: oklch(0.98 0.01 120);
  --sidebar-border: oklch(0.34 0.025 265);
  --sidebar-width: 16.5rem;
  --font-sans: "Geist Variable", ui-sans-serif, system-ui, sans-serif;
  --font-mono: "Geist Mono Variable", ui-monospace, SFMono-Regular, monospace;
}
```

Use 40–44px page titles, 24px major-section titles, 40–44px form controls, 14px body copy, 16–24px major-panel radii, and 10–12px control radii. Make the sidebar dark, the canvas warm, primary buttons chartreuse with dark text, and selected nav items chartreuse. Keep status colors readable and focus rings visible.

- [ ] **Step 4: Update repository design guidance**

Replace the rejected quiet-precision direction in `DESIGN.md` with the architectural control-room tokens, typography, shell, catalog, and motion rules from the spec. Preserve the product scope and implementation constraints.

- [ ] **Step 5: Typecheck the shell**

Run: `cd dashboard && npm run typecheck`
Expected: exit 0 with no TypeScript errors.

### Task 2: Compose the Apps overview and richer catalog

**Files:**
- Modify: `dashboard/src/AppsPage.tsx`
- Modify: `dashboard/src/style.css`

- [ ] **Step 1: Add a data-driven gateway overview**

Compute only real values after `connected`:

```tsx
const discoveredTools = Object.values(toolCounts).reduce((sum, count) => sum + count, 0);
```

Add a presentational `GatewayOverview({ appCount, connectedCount, toolCount })` before `AppSection` and render it before the toolbar when `tab === "all" && !query.trim() && category === "all"`. Its left side contains a mono “GATEWAY / OVERVIEW” label, “Everything your agents need. One secure gateway.” heading, and three stats using the supplied counts. Its right side contains this semantic visual structure:

```tsx
<div className="gateway-map" aria-label="Apps connect through Portunus to agents">
  <div className="map-node map-apps"><Icon name="apps" /><span>Apps</span></div>
  <div className="map-link map-link-left" aria-hidden="true" />
  <div className="map-core"><BrandMark /><span>PORTUNUS</span></div>
  <div className="map-link map-link-right" aria-hidden="true" />
  <div className="map-node map-agents"><Icon name="agents" /><span>Agents</span></div>
</div>
```

Import `BrandMark` alongside `Icon`. Use CSS pseudo-elements for link pulses; do not imply live traffic or add animation under reduced motion.

- [ ] **Step 2: Differentiate Popular from the complete catalog**

Extend `AppSection` with optional props:

```tsx
function AppSection({ title, children, featured = false, count }: {
  title: string; children: ReactNode; featured?: boolean; count?: number;
}) {
  return <section className={`apps-section${featured ? " featured" : ""}`}>
    <div className="apps-section-heading">
      <div><p className="section-kicker">{featured ? "Curated starting points" : "Integration directory"}</p>
      <h2 className="apps-section-title">{title}</h2></div>
      {typeof count === "number" && <span className="section-count">{String(count).padStart(2, "0")}</span>}
    </div>
    <div className="apps-grid">{children}</div>
  </section>;
}
```

Pass `featured count={popular.length}` to Popular and `count={filteredCatalog.length}` to All apps. Add `featured?: boolean` to `CatalogCard`, pass it from Popular, and append ` is-featured` to its card class. Limit visible tags to one category plus two tags so metadata does not overwhelm the card.

- [ ] **Step 3: Recompose card status and action affordance**

Keep all click and keyboard behavior. Use a top row containing `AppBrandMark` and `CardChevron`, then title/status, description, metadata, and tags. Change `CardChevron` to an `arrow-up-right` icon inside a circular affordance. Preserve connected, paused, needs-auth, and tool-count labels.

- [ ] **Step 4: Style the overview, featured section, and working cards**

The overview is a dark 24px-radius panel with subtle grid lines and a lime core. Popular is a separate lavender-ink composition, visually distinct from All apps. All-app cards are white, vertical, at least 230px high, with 48px brand marks and a soft hover shadow. Use three columns on wide screens, two at medium widths, and one below 680px. The toolbar has 44px controls and a large search field; categories remain a separate wrapping row.

- [ ] **Step 5: Build the catalog changes**

Run: `cd dashboard && npm run build`
Expected: TypeScript and Vite complete successfully.

### Task 3: Redesign Keys, drawers, forms, and login

**Files:**
- Modify: `dashboard/src/KeysPage.tsx`
- Modify: `dashboard/src/AppsPage.tsx`
- Modify: `dashboard/src/CreateMcpForm.tsx`
- Modify: `dashboard/src/Login.tsx`
- Modify: `dashboard/src/style.css`

- [ ] **Step 1: Add real key-page context and intentional empty states**

Render a compact `.keys-summary` before the inventory with `keys.length`, “Active credentials”, and a shield icon with “Full access, explicitly granted.” Replace the empty content with:

```tsx
<div className="empty keys-empty">
  <span className="empty-icon"><Icon name="key" size={24} /></span>
  <strong>Your keyring is empty</strong>
  <span>Create a bearer key so agents can call <code>/mcp</code>.</span>
  <button type="button" className="primary sm" onClick={() => setMintOpen(true)}><Icon name="plus" size={14} />Create your first key</button>
</div>
```

Keep the inventory create action and all reveal/copy/delete behavior unchanged. Give the minted-secret banner a dark treatment and preserve token text exactly.

- [ ] **Step 2: Strengthen drawer and custom-form hierarchy**

Wrap the existing drawer header content in a dark `.apps-drawer-header`, keep the close button accessible, and add a mono “APP CONNECTION” eyebrow above the title. Keep provider forms, tools, and lifecycle logic unchanged. In `CreateMcpForm`, prepend `01`, `02`, and `03` spans to the Connection, Authentication, and Routing headings using `.form-section-number`; do not alter form state or submission.

- [ ] **Step 3: Create the split login composition**

Keep `submit` unchanged. Replace the outer content with:

```tsx
<main className="auth-shell">
  <section className="auth-brand-panel">
    <div className="auth-brand"><BrandMark /><span className="name">Portunus</span></div>
    <div className="auth-brand-copy"><p className="auth-eyebrow">MCP CONTROL PLANE</p><h2>Every tool.<br />One gateway.</h2><p>Connect your apps, arm your agents, and keep every credential in one place.</p></div>
    <div className="auth-brand-foot"><span className="environment-dot" />Private by design</div>
  </section>
  <section className="auth-form-panel" aria-labelledby="login-title">
    <div className="auth-card"><p className="auth-eyebrow">Operator access</p><h1 id="login-title">Welcome back.</h1><p className="lede">Enter your password to open the control room.</p>{/* existing form */}</div>
    <p className="auth-foot">Single-operator · Secured session</p>
  </section>
</main>
```

Render the existing password form inside `.auth-card`; preserve its labels, autocomplete, errors, and busy state.

- [ ] **Step 4: Style all secondary surfaces and responsive states**

Use a dark drawer header with light body content, numbered form sections, 40–44px controls, and consistent action alignment. Make the keys table a polished flush desktop table and labeled cards below 680px. Login uses a 42%/58% split on desktop and stacked panels below 760px. All pages must fit a 390px viewport without horizontal scrolling.

- [ ] **Step 5: Typecheck all UI changes**

Run: `cd dashboard && npm run typecheck`
Expected: exit 0 with no TypeScript errors.

### Task 4: Verify and refine the real interface

**Files:**
- Modify only issues exposed by verification in the dashboard files above.

- [ ] **Step 1: Start the real app and isolated local gateway**

Run the dashboard through `npm run dev -- --host 127.0.0.1` and use the same throwaway local gateway approach as the previous UI verification. Do not use production data or bypass the actual React entry point.

- [ ] **Step 2: Inspect desktop and mobile visual compositions**

In ego-browser, capture the authenticated Apps page at 1440×1000 and 390×844, then Keys, an app drawer, custom MCP form, and login. Inspect actual screenshots, not only DOM snapshots. Check that the design reads as intentional at first glance, text fits, popular and all-apps sections are distinct, and the gateway map is balanced.

- [ ] **Step 3: Exercise existing workflows**

Click Apps/Agent keys, All/Connected, search, categories, custom MCP open/close and transport switch, app drawer open/Escape close, API endpoints, key form open/cancel, and temporary key creation/copy. Confirm no console errors and `document.documentElement.scrollWidth <= document.documentElement.clientWidth` at desktop and mobile sizes.

- [ ] **Step 4: Run final checks and clean temporary artifacts**

Run: `cd dashboard && npm run build`
Run from repository root: `git diff --check && git status --short`
Expected: build passes, no whitespace errors, and only intended dashboard/design files changed by this pass. Stop temporary servers and remove temporary verification credentials/databases while preserving existing user files and ignored preview images.
