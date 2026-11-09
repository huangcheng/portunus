#!/usr/bin/env bash
# Generate local secrets for the compose stack: .env, secrets/pepper, secrets/runner.token.
# Refuses to overwrite existing files. Nothing here is ever committed (.gitignore).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

b64() { python3 -c 'import base64,os;print(base64.b64encode(os.urandom(32)).decode())'; }

umask 077
mkdir -p secrets
chmod 0700 secrets  # host-side protection: other host users cannot traverse into it
for f in .env secrets/pepper secrets/runner.token; do
  [[ -e "$f" ]] && { echo "$f already exists; remove it to regenerate" >&2; exit 1; }
done
b64 >secrets/pepper
python3 -c 'import secrets;print(secrets.token_urlsafe(32))' >secrets/runner.token
# Bind-mounted files keep their host mode; the container user (uid 10001) is "other", so the files
# must be o+r. They stay unreachable on the host because secrets/ is 0700 (the Docker daemon, not
# the host user's peers, does the mount). See README "Secret file modes".
chmod 0644 secrets/pepper secrets/runner.token
PASSWORD="$(python3 -c 'import secrets;print(secrets.token_urlsafe(18))')"
cat >.env <<ENV
PORTUNUS_SECRETS_KEY=k1:$(b64)
PORTUNUS_OPERATOR_PASSWORD=${PASSWORD}
PORTUNUS_DEV=1
ENV
echo "Wrote .env, secrets/pepper, secrets/runner.token"
echo "Operator password: ${PASSWORD}"
[[ -e deploy/runner-policy.json ]] || cp deploy/runner-policy.json.example deploy/runner-policy.json
