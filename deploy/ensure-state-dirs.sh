#!/usr/bin/env bash
# Pre-create per-app state directories for plain-stdio children (no skill-bridge
# setup runs for them, and their token caches must survive child restarts).
# Run on every runner host whenever a stdio app adds a skill-state path.
set -euo pipefail
mkdir -p /var/lib/portunus/skill-state/onedrive
chmod 777 /var/lib/portunus/skill-state/onedrive
echo "state dirs ready"
