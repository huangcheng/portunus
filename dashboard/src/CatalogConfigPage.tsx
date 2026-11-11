import { useMemo, useState } from "react";
import { api } from "./api";
import { CatalogConfigForm } from "./CatalogConfigForm";
import { AppBrandMark, appIconIdForSlug } from "./icons/apps";
import { Icon } from "./icons/ui";
import type { CatalogApp } from "./providers";

type Props = {
  catalog: CatalogApp[];
  onChanged: () => void | Promise<void>;
};

/** Operator CRUD for Apps catalog recipes (config-driven Connect entries). */
export function CatalogConfigPage({ catalog, onChanged }: Props) {
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState<CatalogApp | null>(null);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const list = [...catalog].sort(
      (a, b) => a.sort_order - b.sort_order || a.label.localeCompare(b.label),
    );
    if (!q) return list;
    return list.filter((a) => {
      const hay = [a.label, a.name, a.slug, a.connection, a.description, ...a.categories, ...a.tags]
        .join(" ")
        .toLowerCase();
      return hay.includes(q);
    });
  }, [catalog, query]);

  const showForm = creating || !!editing;

  async function handleDelete(app: CatalogApp) {
    if (!window.confirm(`Delete catalog app “${app.label}” (${app.slug})? Connected servers are not removed.`)) {
      return;
    }
    setError("");
    setDeletingId(app.id);
    try {
      await api.deleteApp(app.id);
      if (editing?.id === app.id) setEditing(null);
      await onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setDeletingId(null);
    }
  }

  return (
    <div className="catalog-config-page stack">
      {error && <div className="notice error">{error}</div>}

      {!showForm && (
        <>
          <div className="catalog-config-toolbar">
            <div>
              <p className="section-kicker">Catalog</p>
              <h2 className="apps-section-title">Config-driven apps</h2>
              <p className="desc">
                Edit recipes that power Connect. This does not create a live MCP server.
              </p>
            </div>
            <button
              type="button"
              className="primary sm"
              onClick={() => {
                setEditing(null);
                setCreating(true);
              }}
            >
              <Icon name="plus" size={14} />
              New catalog app
            </button>
          </div>

          <label className="field apps-search catalog-config-search">
            <span className="sr-only">Search catalog</span>
            <span className="search-control">
              <Icon name="search" size={14} />
              <input
                placeholder="Search catalog"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                autoComplete="off"
              />
            </span>
          </label>

          {filtered.length === 0 ? (
            <p className="empty">No catalog apps match.</p>
          ) : (
            <ul className="catalog-config-list">
              {filtered.map((app) => (
                <li key={app.id} className="catalog-config-row">
                  <div className="catalog-config-row-main">
                    <AppBrandMark id={appIconIdForSlug(app.slug)} />
                    <div>
                      <div className="catalog-config-row-title">
                        {app.label}
                        {app.popular ? <span className="badge">Popular</span> : null}
                      </div>
                      <div className="catalog-config-row-meta">
                        <span className="mono">{app.slug}</span>
                        <span>{app.connection}</span>
                        <span>sort {app.sort_order}</span>
                      </div>
                      {app.description ? <p className="catalog-config-row-desc">{app.description}</p> : null}
                    </div>
                  </div>
                  <div className="catalog-config-row-actions">
                    <button
                      type="button"
                      className="outline sm"
                      onClick={() => {
                        setCreating(false);
                        setEditing(app);
                      }}
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      className="ghost sm"
                      disabled={deletingId === app.id}
                      onClick={() => void handleDelete(app)}
                    >
                      <Icon name="trash" size={14} />
                      Delete
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </>
      )}

      {showForm && (
        <div className="card create-card">
          <h2>{editing ? `Edit ${editing.label}` : "New catalog app"}</h2>
          <p className="desc">
            {editing
              ? "Update identity and spec. Slug and connection stay fixed."
              : "Create a catalog recipe. Operators connect it from the Apps directory."}
          </p>
          <CatalogConfigForm
            initial={editing}
            onSaved={async () => {
              setCreating(false);
              setEditing(null);
              await onChanged();
            }}
            onCancel={() => {
              setCreating(false);
              setEditing(null);
            }}
          />
        </div>
      )}
    </div>
  );
}
