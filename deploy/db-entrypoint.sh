#!/bin/bash
set -euo pipefail
[[ "${JAKH_DATABASE:-}" =~ ^[a-z][a-z0-9_]{0,62}$ ]] || { echo 'Invalid customer database name' >&2; exit 1; }
[[ "${JAKH_DB_USER:-}" =~ ^[a-z][a-z0-9_]{0,62}$ ]] || { echo 'Invalid customer database role' >&2; exit 1; }
[[ "$JAKH_DATABASE" != 'postgres' && "$JAKH_DATABASE" != 'template0' && "$JAKH_DATABASE" != 'template1' ]] || { echo 'A dedicated customer database is required' >&2; exit 1; }
[[ "$JAKH_DB_USER" != 'jakh_cluster_admin' && "$JAKH_DB_USER" != 'postgres' ]] || { echo 'Application must not use cluster administrator' >&2; exit 1; }
export JAKH_INITIAL_APP_PASSWORD="$(</run/secrets/db_password)"
[[ ${#JAKH_INITIAL_APP_PASSWORD} -ge 32 ]] || { echo 'Strong customer database secret required' >&2; exit 1; }
exec /usr/local/bin/docker-entrypoint.sh postgres
