/** Curated Apps catalog: Connect creates/updates a remote Server; operator supplies client id/secret. */

export type WellKnownApp = {
  id: string;
  label: string;
  name: string;
  slug: string;
  description: string;
  url: string;
  authorize_url: string;
  token_url: string;
  scopes: string[];
  popular?: boolean;
};

export const WELL_KNOWN_APPS: WellKnownApp[] = [
  {
    id: "dropbox",
    label: "Dropbox",
    name: "Dropbox",
    slug: "dropbox",
    description: "Files, sharing, and file requests via Dropbox MCP.",
    url: "https://mcp.dropbox.com/mcp",
    authorize_url: "https://www.dropbox.com/oauth2/authorize",
    token_url: "https://api.dropboxapi.com/oauth2/token",
    scopes: [
      "account_info.read",
      "files.metadata.read",
      "files.content.read",
      "files.content.write",
      "sharing.read",
      "sharing.write",
      "file_requests.read",
      "file_requests.write",
    ],
    popular: true,
  },
];

export function appById(id: string | null): WellKnownApp | undefined {
  if (!id) return undefined;
  return WELL_KNOWN_APPS.find((a) => a.id === id);
}
