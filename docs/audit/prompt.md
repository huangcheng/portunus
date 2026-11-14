# Role
Audit the attached Portunus design spec only. Do not write code. Do not call tools.

Portunus = self-hosted MCP gateway (Python/FastAPI): Connect recipes, vaulted secrets, stdio/remote MCP, skill-bridge CLIs, multi-runner fleet.

# Required output format
1. Verdict: GO / GO WITH FIXES / NO-GO — one sentence
2. Critical risks (catalog↔server split, on-demand installer security, multi-arch, migration off catalog_apps, skill-bridge inject, Connect UX)
3. Contradictions or stale claims
4. Missing requirements (error contracts, runner trust boundary, tree vs install_script, SPA id/slug)
5. Architecture/design notes (who runs the install script — gateway vs runner; locking; airgap)
6. Top 5 concrete edits

Under 450 words. Be skeptical and specific.
