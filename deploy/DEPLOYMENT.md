# JAKH Odoo deployment foundation

This is a staged, installable foundation, not a certified production ERP. No public website,
cloud account, DNS record, paid service, or existing JAKH customer database was changed.
The supplied Odoo archive was verified against the exact official HTTPS artifact before
execution: 421,913,080 bytes, SHA256
`53b2b33a58b9e0f743d2076b01b5a955a867c044de232aed109d838e45018961`.
The reviewed versions and source references are in `../vendor.lock.json`.

## Historical original-host staging

This section records the original workspace layout and its private evidence. For
a new repository-root checkout, follow `RELOCATION.md`; do not copy these absolute
runtime configurations or assume the original `outputs/` wrapper exists.

The macOS staging runtime is entirely under workspace `work/odoo-build/`: verified source,
isolated Python 3.12 venv, locally compiled PostgreSQL 16.15, new synthetic databases and
private secrets. No global installations were needed. The PostgreSQL build uses existing
Apple developer tools and has no TLS/readline/ICU/zlib support; it is **localhost staging
only**, not the production database distribution. The Python native exception uses
`psycopg2-binary==2.9.9` instead of the same-version source distribution because this
bundled runtime lacks OpenSSL linker libraries. The container uses the original Odoo
requirements with Debian development libraries.

Two customers have distinct database roles, databases, configurations, session storage
and filestore roots. Customer A serves localhost 8810; customer B serves localhost 8811;
the staging PostgreSQL listener is localhost 17816. Existing JAKH on 8792 is preserved.
Synthetic staging owners are not the real owner's activated identity. Staging secrets
stay in owner-only files under `work/`; never put them in this deliverable or a ZIP.

Runtime commands use `scripts/native_staging.py`, with an explicit staging root:

```sh
work/odoo-build/venv/bin/python outputs/jakh-erp-odoo/scripts/native_staging.py serve --root work/odoo-build/staging-local --customer a
work/odoo-build/venv/bin/python outputs/jakh-erp-odoo/scripts/native_staging.py serve --root work/odoo-build/staging-local --customer b
```

Serve refuses to bind HTTP before offline enrollment replaces default `admin` credentials.
Stop the specific foreground Odoo process using Ctrl+C. `pg-start` and `pg-stop` act only
on the exact cluster in the validated staging marker; stopping PostgreSQL also disconnects
both synthetic customers. Do not stop the cluster while the previews are in use.

Tagged addon tests can run **only against synthetic staging**, with its Odoo HTTP process
stopped. The `test` action updates the named modules offline and runs `--test-tags jakh`;
inspect its private log for assertions, not just the process exit code. It is not a
production migration or update command.

### Arabic/English lean runtime

`scripts/build_lean_runtime.py` makes a **new** runtime tree; it never removes or edits the
verified upstream source, installed records, or current customer configurations. With
`--native-root`, it reads the actual installed-module inventory from both isolated
customers in read-only database transactions. With `--profile`, it uses the reviewed
deployment inventory at `deploy/runtime-modules.json`. It includes the complete declared
dependency closure, not just addons that have visible menu entries.

The runtime excludes uninstalled addons, non-Arabic/English translation catalogs,
non-Arabic/English PDF viewer locales and generated caches. The vendor calendar locale
bundle is narrowed to its existing Arabic/English definitions, without rewriting their
translations. The PDF viewer's Arabic short-code and required English `en-US` fallback
are retained. Core locale/country/currency data, all licenses, framework functions,
tests, migrations and manifest-referenced files remain. Such technical data is not an
extra selectable UI language. Installed dependency addons must not be deleted merely
because their menu or an external service is disabled.

```sh
work/odoo-build/venv/bin/python -B outputs/jakh-erp-odoo/scripts/build_lean_runtime.py \
  --source work/odoo-build/source/odoo-20.0.post20261012 \
  --addons outputs/jakh-erp-odoo/addons --native-root work/odoo-build/staging-local \
  --destination work/odoo-build/runtime-ar-en-NEW
```

The output contains `vendor/`, `addons/`, `bootstrap_addons/`, and `runtime-manifest.json` with inventory,
inclusion/exclusion evidence, byte totals and a SHA-256 hash per packaged file. Complete
translation changes before building; never manually patch an already-built runtime.

`bootstrap_addons/` contains only generated, uninstallable category manifest dictionaries,
not omitted operational modules or languages. Native fresh database initialization creates
category identifiers from these metadata dictionaries before loading retained base data.
The container validates them and includes this path only for explicit offline `init`;
ordinary `serve` excludes it. Operational module inventory and dependency closure are
unchanged. No upstream source patch or full-source serving image is required.

Stop **both** isolated customer web services before activation. The native RTL compiler
must be installed and passed as a directory containing an executable `rtlcss`. The
activation command verifies every packaged file and the current installed-module
inventory, creates new private configurations and atomically changes runtime paths:

```sh
work/odoo-build/venv/bin/python -B outputs/jakh-erp-odoo/scripts/native_staging.py \
  activate-runtime --root work/odoo-build/staging-local \
  --runtime work/odoo-build/runtime-ar-en-NEW --rtlcss-bin /path/to/rtlcss/bin
```

Run the tagged offline tests for each customer and restart the previews only after they
pass. The original source and configuration paths remain recoverable; `restore-runtime`
with the same staging root restores those paths while both web services are stopped.
Neither switching nor restoring runtime paths modifies or rolls back any database.
Retest any schema changes separately before a source rollback.

If installing the official RTL compiler is externally blocked, the explicit
`--allow-missing-rtlcss` exception permits **synthetic staging only** to continue. It
records `rtlcssReady: false` and prints a readiness warning. Translation improvements
and packaging can then be tested, but Arabic layout and production readiness must not
be approved until the real compiler is available and native RTL assets are regenerated.

On 2026-10-13 the npm registry timed out, but official upstream tagged source files were
available. The native compiler is now installed and its actual conversion through the
pinned framework configuration passes. `deploy/rtlcss` contains seven small source-built
package archives, upstream licenses, per-file source URLs/hashes and a **real npm-generated
local-archive lock**. This is not a claim that these are official npm registry archives.
RTLCSS 4.3.0 source is pinned to commit `f1a08a67fc536cf84e11736fbb0f1ae0221e3d11`.
Dependencies are PostCSS 8.5.6, nanoid 3.3.11, source-map-js 1.2.1, Escalade 3.1.1,
picocolors 1.1.1 and strip-json-comments 3.1.1; npm validates their compatible ranges.
Only Escalade required a mechanical build: preserved upstream ES modules are converted
by exact whole-line import/export substitutions, with original/generated hashes and
focused sync/async tests. No RTL compiler behavior was reimplemented. These are runtime
source packages; unavailable optional declarations are recorded, not advertised as a
complete TypeScript development distribution. They still require production dependency
and vulnerability review.

Install into a new workspace-local directory without network or lifecycle scripts:

```sh
work/odoo-build/venv/bin/python -B outputs/jakh-erp-odoo/scripts/install_rtlcss.py \
  --destination work/odoo-build/rtlcss-native-NEW
```

The installer checks every archive against its SHA-512 npm lock and every packed source
file against the source/build SHA-256 receipts before running `npm ci --offline`.
Activate the immutable application snapshot with that directory's `node_modules/.bin`.
Activation verifies the exact compiler version and actual left-to-right to right-to-left
conversion through the pinned native `rtlcss.json` before recording readiness.
While each customer preview is stopped, rebuild generated native assets:

```sh
work/odoo-build/venv/bin/python -B outputs/jakh-erp-odoo/scripts/regenerate_rtl_assets.py \
  --root work/odoo-build/staging-local --customer a
work/odoo-build/venv/bin/python -B outputs/jakh-erp-odoo/scripts/regenerate_rtl_assets.py \
  --root work/odoo-build/staging-local --customer b
```

This uses the native generated-assets boundary, not business attachments, and explicitly
builds RTL bundles (the generic native pre-generator skips RTL). The transaction is
committed only after CSS compilation succeeds. `--check-only` tests the compiler without
opening a database. Native asset URLs hash source metadata, not compiler versions:
unchanged source may require a hard reload after regeneration; a reviewed source revision
and rebuilt immutable snapshot changes the bundle URL. Do not wipe general browser data.
Verify actual computed RTL layout, keyboard and mobile operation after deployment.

## Container installation, cloud or on-premise

The Compose package is designed for a host with Docker Engine and Compose v2. Neither is
installed on this Mac, so the container build and Keycloak profile are not runtime-tested
here. The Odoo source is checksum-pinned. Python, PostgreSQL and Keycloak images are
immutable-digest pinned after independently hashing the official registry manifests.
Perform a dependency vulnerability scan before production. Base operating-system packages and upstream Python transitive dependencies
also require a reproducible production dependency review.

The Docker build uses the same lean-runtime builder and checked-in module profile.
Full upstream source and native build tools stay in a disposable build stage, not the
application image. The final image uses `/opt/jakh-runtime/vendor` and
`/opt/jakh-runtime/addons`. Refresh and review `runtime-modules.json` before enabling a
new module or another country localization, then rebuild and test the image. The profile
is evidence of currently installed capabilities, not a claim that every ERP requirement
or future localization is complete.

The container build installs this same reviewed local-archive compiler lock with
`npm ci --offline`, lifecycle scripts disabled, and all seven archives copied into the
build context. It includes Node.js at runtime and exposes the compiler on `PATH` for the
native asset pipeline. The offline compiler installation and conversion were tested
natively, including a fresh empty npm cache. The full container integration remains
source-validated only because Docker is unavailable here; build/test the image before
deployment. OS/Python installation still needs its normal package sources.

Run a separate Compose project for each customer, with its own volumes and secret
directory. Do not reuse project names, database volumes or filestore volumes. Docker
container names and networks are project-scoped. Both DB listeners are internal only.
The Odoo process runs UID/GID 101 after a narrow root setup step reads private secrets,
owns only its data/configuration directories, drops supplementary groups and drops UID.
The application role is explicitly `NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT`;
cluster bootstrap credentials are separate and never supplied to Odoo.

Generate secrets in a **new private directory outside the source tree**:

```sh
python3 scripts/generate_secrets.py --destination /your/private/new-customer-secrets
```

Choose explicit names and owner identity; passwords have no built-in values:

```sh
export COMPOSE_PROJECT_NAME=jakh_customer_acme
export JAKH_DATABASE=customer_acme
export JAKH_DB_USER=customer_acme_app
export JAKH_OWNER_LOGIN=your-verified-owner@example.com
export JAKH_SECRETS_DIR=/your/private/new-customer-secrets
export JAKH_HTTP_PORT=8810
docker compose -f deploy/compose.yaml build app
docker compose -f deploy/compose.yaml up -d db
docker compose -f deploy/compose.yaml run --rm --no-deps app init
docker compose -f deploy/compose.yaml up -d app
```

The offline `init` command accepts an empty database only, installs JAKH addons, replaces
the default admin identity/password and writes an enrollment marker before HTTP can run.
It does not import the real owner's old password hash or prove that email belongs to the
operator. A production owner must explicitly receive/activate their own credential through
an approved, verified enrollment process. Remove or rotate the initialization owner secret
after enrollment. Existing database password rotation and addon upgrades require separate,
reviewed runbooks; regenerating secret files does **not** rotate passwords in an existing DB.

The database initializer runs only for a new PostgreSQL volume. Never use
`docker compose down --volumes` on a customer installation. No destructive reset script
is provided. Keep each customer's `database` **and** `filestore` volumes together.

## Website hosting later

Default ports bind to `127.0.0.1`, not the public Internet. For a website, keep this private
backend and put a reviewed TLS reverse proxy on the same host/private network. Use one
customer hostname routed to exactly one customer instance. Deny public access to
`/web/database/*`, protect administrative enrollment, set strict request/body/time limits,
and set `JAKH_PROXY_MODE=true` **only** when all traffic arrives from that trusted proxy.
Odoo's exact `db_name`, anchored `dbfilter` and `list_db=False` remain mandatory; headers
or browser query strings must not be used to choose arbitrary customer databases.
Do not enable proxy mode on an untrusted direct/public backend listener.

Before public launch: HTTPS/session-cookie verification, MFA enrollment and recovery,
least-privilege user acceptance, rate limits, email sender verification, backup/restore
drill, monitoring, patched PDF generation and Arabic RTL assets, data-residency agreement,
and load/security tests. Wkhtmltopdf is not supplied by this minimal container; the RTL
compiler is now declared but its container installation/execution remains unverified.
Both must be reviewed for the pinned Odoo version. Do not claim tested PDF/Arabic
print output. Optional integrations/localization (P6, Jisr, ZATCA/UAE tax) remain separate
acceptance milestones; selecting a company country/currency is not tax certification.

## Optional identity provider

The disabled-by-default `sso` profile provides Keycloak 26.8.0 and a separate PostgreSQL
database. Its `start-dev` command and localhost 8081 port are an **identity sandbox only**.
There is no automatic Odoo OIDC provider, cross-customer platform switch, or activated
real-owner SSO claim. A production Keycloak deployment needs `start`, TLS/hostname settings,
reviewed realm/client claims, account linking, logout, MFA and recovery acceptance tests.

Keycloak runs as UID 1000. Compose file-backed secrets may ignore requested UID/mode;
the profile fails closed if its private secret files are unreadable. On a Linux deployment,
the operator must provision those two files as UID 1000, mode 0400, in a protected directory,
or use a managed secret mechanism that honors container ownership. Do not solve this by
making host secret files world-readable. Enabling the profile is optional and must not
block native Odoo authentication.

## Backups and migration boundaries

A valid customer snapshot includes a PostgreSQL consistent dump, matching filestore,
installed addon/source versions, and approved metadata. Coordinate maintenance so no
Odoo/cron writes or filestore garbage collection can occur during the snapshot; a live
directory copy alone is not a consistent backup. Restore into an isolated new DB and
filestore, verify attachments, identities, permissions, balances and workflow states,
then perform a controlled cutover. Never overwrite a live customer by script.

Do not batch-copy legacy accounts, password hashes, browser storage or ambiguous monetary
strings. The legacy app remains the source system until reviewed record mappings and
opening balances reconcile. This package provides staging and deployment groundwork,
not completed statutory implementation or migration acceptance.

Primary references: [Odoo 20 source](https://nightly.odoo.com/20.0/nightly/src/),
[Odoo source installation](https://www.odoo.com/documentation/20.0/administration/on_premise/source.html),
[PostgreSQL 16 installation](https://www.postgresql.org/docs/16/install-requirements.html),
[Keycloak container guide](https://www.keycloak.org/getting-started/getting-started-docker).
