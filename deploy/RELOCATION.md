# Move JAKH ERP to another laptop or account

The GitHub repository carries the maintained application source and handover documentation. It does not carry this laptop's running databases, retained demo changes, attachments, credentials or chat history. Choose a fresh installation or a controlled transfer of existing state before preparing the destination.

The repository is published with `addons/`, `scripts/`, `deploy/`, `requirements/` and `tests/` at its root. All commands below run from that repository root in a POSIX shell, such as Linux, macOS or WSL. Do not add the original workspace's `outputs/jakh-erp-odoo` prefix.

## 1. Clone the source

After the repository owner publishes the source and grants any required GitHub access:

```sh
git clone https://github.com/jame17291-sys/JAKHERP
cd JAKHERP
git rev-parse HEAD
git status --short
```

Record the commit used for the destination. If a particular release commit is supplied, use that reviewed revision rather than assuming the branch tip is the accepted build. Authenticate to a private repository through the destination's approved GitHub mechanism; never put a token into the clone URL, documentation or chat.

Start with these tracked documents:

- [README](../README.md): operating flow, implemented scope and engineering evidence.
- [Deployment foundation](DEPLOYMENT.md): container configuration, isolation, enrollment and production boundaries.
- [English/Arabic demo walkthrough](demo-walkthrough.html): retained synthetic workflow and practice records.
- [15-step completion register](../requirements/construction-completion-15.json), [SOW acceptance matrix](../requirements/acceptance-matrix.json) and [traceability register](../requirements/traceability.json): remaining customer and release acceptance.
- [Staging verification](staging-verification.json): historical local evidence and its limits.
- [Vendor lock](../vendor.lock.json), [runtime module profile](runtime-modules.json), [JAKH Python dependency lock](requirements-jakh.lock.txt), [license](../LICENSE) and [copyright notices](../COPYRIGHT): exact source/dependency basis and preserved notices.

Some evidence paths refer to private `work/` directories on the original machine. Their presence in a register does not mean those files are in GitHub or have been reproduced on the destination.

## 2. Know what must move separately

| Material | GitHub source | Destination handling |
|---|---|---|
| JAKH addons, scripts, tests, requirements, guides, source locks and notices | Included | Clone the reviewed commit; retain licenses and provenance. |
| Upstream Odoo archive, Python environment, compiled PostgreSQL and generated runtime/assets | Rebuilt or obtained from the pinned sources | Do not copy a macOS virtual environment or compiled database binaries to another operating system. Docker builds obtain and verify the pinned Odoo archive. |
| Existing company records, users, posted journals, practice changes and demo manifest stored in the database | Not included | Preserve through a consistent database backup when transferring current state. A fresh install starts with a new database. |
| Business attachments and generated exports in the matching filestore | Not included | Transfer together with the matching database, privately and encrypted. |
| Passwords, private configuration, API keys, invitation links, signing/encryption material and private demo-manifest exports | Not included | Use an approved encrypted secret channel or secret manager outside the checkout. Do not attach them to a chat or GitHub issue. |
| Browser sessions, local cookies, assistant account history and machine-specific absolute paths | Not a portable application backup | Sign in separately and reconstruct destination configuration through the approved process. |

The repository ignore rules are a safeguard, not a security review of an upload. Inspect staged files before every publication. Do not add `work/`, secret directories, `.env` files, private `.conf` files, database dumps, filestore archives, logs containing credentials or an unreviewed whole-workspace ZIP.

## 3. Fresh container installation on the new laptop

This route creates a new, empty customer installation. It does not transfer the retained Saudi/UAE previews or automatically create their construction demo.

Prerequisites are Docker Engine with Compose v2, a Python 3.12+ executable for the local secret generator, sufficient disk space, and access to the declared image/package sources. The examples use `python3.12`. Supply an equivalent Python 3.12+ executable if that name is different. The `up --wait` command below requires a Compose version supporting that option.

The container definition has been source-validated, but the original Mac did not have Docker. Its image build, fresh container enrollment, container PDF/RTL execution and operational deployment still require validation on this host. Native Mac test results do not establish container acceptance. Preserve the pinned image digests and source checksum while investigating a build failure.

Choose a new Compose project, database, application role, localhost port and private secret directory. The secret directory's parent must already exist, and the directory itself must be new and outside the checkout. The example owner login is a synthetic identity; replace it through a verified enrollment decision if installing for a real owner.

```sh
export COMPOSE_PROJECT_NAME=jakh_relocation_review
export JAKH_DATABASE=jakh_review
export JAKH_DB_USER=jakh_review_app
export JAKH_OWNER_LOGIN=owner@jakh-review.test
export JAKH_SECRETS_DIR=/absolute/private/jakh-relocation-secrets
export JAKH_HTTP_PORT=8810
export JAKH_PROXY_MODE=false

python3.12 scripts/generate_secrets.py --destination "$JAKH_SECRETS_DIR"
docker compose -f deploy/compose.yaml config --quiet
docker compose -f deploy/compose.yaml build app
docker compose -f deploy/compose.yaml up -d --wait db
docker compose -f deploy/compose.yaml run --rm --no-deps app init
docker compose -f deploy/compose.yaml up -d app
docker compose -f deploy/compose.yaml ps
```

Replace `/absolute/private/jakh-relocation-secrets` before running the commands. The generator refuses an existing destination and prints no secret values. Do not run a second generator over an existing deployment or assume changing a file rotates a database password.

Open `http://127.0.0.1:8810/jakh` on this laptop, using the port selected above. Retrieve the generated initial owner credential privately through the approved local secret-handling process. Do not paste it into the assistant conversation. A different ChatGPT or GitHub account is not an application owner account and does not activate one.

The following boundaries remain part of this installation:

- Use one distinct Compose project, secret directory, database and filestore volume per customer. The database role is a dedicated non-superuser; PostgreSQL is not published to the host.
- `app init` is an explicit offline operation for an empty database only. It installs the addons, replaces default administrator credentials and records enrollment before HTTP serving. It refuses an already populated database; it is not an upgrade, restore or password-reset command.
- The owner login/password setup does not verify email ownership or send an activation message. Complete real-owner verification and credential rotation through an approved process.
- The application publishes only to `127.0.0.1`. Keep proxy mode false for direct local use. Public hosting requires the separate TLS/proxy, identity, residency and operational gates in `DEPLOYMENT.md`.
- The optional `sso` profile is disabled. Its Keycloak development sandbox is not accepted production SSO or MFA.
- Keep `database` and `filestore` volumes together. Do not use `docker compose down --volumes`, database deletion or volume removal to resolve an initialization problem.

To stop only this new project's application while keeping its data:

```sh
docker compose -f deploy/compose.yaml stop app
```

Rebuilding an image or pulling source does not migrate database schemas or restore earlier data. Addon upgrades, password rotation and rollback need their own reviewed procedure and recoverable state snapshot.

## 4. Recreate a synthetic demo deliberately

The retained demo is database state. A Git clone and fresh container initialization do not include the existing 11-villa records, pending practice changes or completed reference project.

The current `scripts/seed_construction_demo.py` is a native staging tool. It requires an enrolled, explicitly marked synthetic customer `a` or `b`, exact synthetic company/country/currency identity, validated staging metadata, the declared native Python/source/PostgreSQL paths, and stopped customer/test HTTP services with quiet database connections. It runs the bounded scenario through normal role-controlled actions in one transaction and refuses unmarked collisions. An identical retry validates and returns the saved scenario; it never resets practice work.

Native staging now supports the repository-root layout: a standalone clone defaults to `JAKHERP/work/`. The original `outputs/jakh-erp-odoo` layout continues to use its existing workspace-level `work/`, preserving that installation's state. Each staging root must still be a specific, newly prepared child of the appropriate `work/` directory.

The original native runtime was exercised on macOS. Its staging metadata contains absolute Python, source, PostgreSQL, configuration and filestore paths, and its binaries are machine-specific. Do not copy that metadata or runtime environment to the new laptop as a working installation; prepare and validate destination-native paths and compatible dependencies. Repository-root support does not remove the staging identity, enrollment or stopped-service checks. The container entrypoint exposes `init` and `serve`, not a demo-seed mode. **There is no validated one-command Docker demo recreation in this release.**

For a native environment, install the reviewed upstream Odoo Python requirements and the mandatory `deploy/requirements-jakh.lock.txt` into the same isolated Python environment before loading or initializing the JAKH addons. `jakh_operations` declares the Python external dependency `websocket`, supplied by the locked `websocket-client` wheel. The dependency is required for addon loading even when the governed Chromium PDF transport is not used. The command shape, from the repository root, is:

```text
<validated-native-python> -m pip install --no-deps --only-binary=:all: --require-hashes -r deploy/requirements-jakh.lock.txt
```

Use that same environment for module installation/upgrades, native tests, seeding and serving; do not install the dependency globally or replace the pinned wheel without review. The Dockerfile already installs this dependency lock during its image build; that does not replace validation of the completed container.

For a new synthetic demo, first establish and validate a fresh, isolated staging runtime for the destination platform, or implement and test a narrowly scoped container-compatible seeding entry point. Do not copy/edit the old staging marker, `.owner-enrolled` file or demo parameters merely to bypass a refusal. Do not aim the seeder at production, enable real transport credentials or use synthetic values as opening balances.

After that prerequisite is accepted, use the seeder only with that approved runtime and its actual marked root. The command shape is:

```text
<validated-native-python> scripts/seed_construction_demo.py --root <validated-marked-staging-root> --customer a
```

This is a placeholder showing the required inputs, not a command for the fresh Docker installation above. Follow [the walkthrough](demo-walkthrough.html) once the dataset is successfully prepared. `/jakh/demo` is intentionally hidden unless the signed-in user, selected synthetic company, database-bound manifest and record permissions qualify. Transient report forms may expire; native report criteria can generate a new snapshot from retained sources, while the protected saved workbook persists.

## 5. Transfer the current data when continuity is required

To preserve the current demo and every later practice change, use a controlled state transfer instead of reseeding. Before exporting, identify the authorized source customer, destination machine/owner, an explicit encrypted destination outside GitHub, and the approved recovery-key channel. If no encrypted destination has been selected, complete the source handover and leave current state on the original laptop.

The transfer package must contain a consistent PostgreSQL logical dump and its matching business filestore from the same coordinated write-free snapshot, together with the source commit, installed module versions, database/company/currency identity, attachment inventory and integrity hashes. Keep each customer's snapshot and permissions separate. Stop or otherwise exclude application writes, background jobs and filestore garbage collection during that coordinated snapshot. Copying live directories or only the database is not sufficient.

Encrypt the backup at rest and in transit with an approved tool and separately controlled recovery key. Keep backup files, keys, private configuration and credentials out of GitHub, source ZIPs, chat uploads and issue attachments. Document who may decrypt the package and where the encrypted recovery copy is retained.

Restore first into a new isolated destination database and filestore using compatible PostgreSQL/Odoo versions and the matched code. A logical PostgreSQL backup is the transfer boundary; raw macOS PostgreSQL data directories and compiled binaries are not a cross-platform migration. Reconcile source identifiers, attachment content, users/roles, company isolation, posted debit/credit totals, residuals and workflow states before considering cutover. Keep live integrations, mail delivery and scheduled jobs disabled during recovery validation.

The current native recovery rehearsal is bounded to marked synthetic staging. It creates a separate recovery copy, does not enroll or serve that copy, and is not a generic migration service. The container entrypoint has no restore/enroll-existing-database mode: `app init` must not be run over a restored populated database. Serving restored state needs a reviewed destination configuration/enrollment procedure, verified database credentials and routing, and the appropriate application data layout. Do not forge an enrollment marker to make a restore boot.

The demo manifest is bound to its database and synthetic company identity. A renamed database or rebranded company can correctly make `/jakh/demo` unavailable. Address any required rebinding through the reviewed restore plan and normal access checks; do not weaken the route guards.

No transfer, public traffic switch, deletion, uninstall or cleanup of the original laptop is authorized by this guide. Keep the source installation and its private state recoverable until the destination has been tested, its recovery accepted and any cutover explicitly authorized. A source-code rollback or `restore-runtime` changes application paths; it does not undo database changes.

## 6. Acceptance on the destination

Record the destination OS/architecture, Docker/Compose versions, source commit, image build outcome and actual checks performed. Keep secrets out of logs and evidence. Before relying on the relocated installation, verify:

1. Pinned image/source acquisition, clean build, fresh initialization and restart with retained volumes; failures remain visible rather than being fixed by deleting data or changing locks without review.
2. Native sign-in and verified owner enrollment, exact single-customer routing, dedicated database permissions, denied anonymous/internal access and role/company restrictions. Technical-office users must not gain financial fields.
3. The single Modules column, native navigation, English/Arabic preference persistence and actual RTL layout on desktop and phone-sized views.
4. The agreed contracting flow in isolated synthetic data, including independent approvals, controlled stock/work quantities, balanced postings and source-linked reporting. Run the relevant native tagged tests on an explicitly isolated test target; do not run installation tests over transferred customer data.
5. Actual English/Arabic PDF/XLSX generation in the container, including fonts, supported renderer/sandbox behavior, RTL, totals and source permissions. The Dockerfile includes Chromium and the pinned RTL compiler; their presence alone is not passing export evidence. Native invoice rendering and the maintained JAKH renderer have different coverage.
6. A matched database-plus-filestore backup/restore drill with attachments, permissions and financial reconciliation. Set measured backup retention, recovery location and recovery responsibilities for the intended deployment.

Historical native regression counts and synthetic recovery evidence remain useful context. They do not automatically accept this laptop, Docker runtime, customer migration, public hosting, production SSO/MFA, external vendor/bank transport, security review, availability/RPO/RTO or UAE/KSA tax/e-invoicing compliance. Keep the tracked customer/UAT and production sign-off fields unaccepted until authorized evidence and human approvals exist.

## Resume prompt for another assistant account

Clone the repository first, open that checkout in the destination workspace, then paste this prompt. It supplies context without transferring credentials or relying on the original chat history.

```text
Continue the JAKH ERP source project from https://github.com/jame17291-sys/JAKHERP on this laptop. The repository root contains addons/, scripts/, deploy/, requirements/ and tests/. Inspect the checkout, current commit, local instructions and destination environment first.

Read README.md, deploy/RELOCATION.md, deploy/DEPLOYMENT.md, deploy/demo-walkthrough.html, vendor.lock.json, deploy/runtime-modules.json, deploy/requirements-jakh.lock.txt, deploy/staging-verification.json, requirements/construction-completion-15.json, requirements/acceptance-matrix.json and requirements/traceability.json. Treat private evidence paths from the original machine as historical references, not accessible or freshly reproduced proof.

Help me establish a reproducible local installation and verify it on this host. Use a new isolated target for testing. Preserve native Odoo authentication, role/company isolation, accounting and approval controls, the single Modules column, branded teal/white UI, native English/Arabic and all original licenses/notices. Preserve unrelated changes and keep edits scoped to the requested relocation work.

First distinguish fresh source installation, synthetic demo recreation and transfer of existing state. The Git clone contains no current database, filestore, secrets or previous chat context. The Docker package still needs actual build/runtime/export acceptance on this host. Its init operation is only for an empty database. Native staging supports repo-local work/ while retaining the original outputs-layout work path, but destination staging metadata and binaries must be prepared for this machine. Install deploy/requirements-jakh.lock.txt alongside the upstream Odoo requirements in the native environment: jakh_operations requires its declared websocket dependency. Docker installs that lock during the image build. The demo seeder still requires marked, enrolled and stopped synthetic staging; do not claim a Docker demo-seed or restored-database enrollment workflow exists without implementing and validating it in authorized isolated scope.

Do safe source/environment checks and prepare a concrete test plan. If existing data is to move, establish the exact authorized source and encrypted destination first, then use a matched PostgreSQL logical backup plus filestore and a separately protected recovery key. Do not overwrite a live database, forge a staging/enrollment marker, reset demo work, remove Docker volumes, delete laptop files or switch public traffic. Request the specific missing destination or cutover authority when it is actually needed.

Never ask me to paste passwords, API keys, GitHub tokens, private invitations, encryption keys, database dumps or filestore archives into chat or GitHub. Use approved local secret files or a secret manager. Do not enable real external email, bank, P6, Jisr or other transports from synthetic examples. Synthetic financial values are not opening balances.

Verify changes with meaningful checks and report what passed, what remains unverified and where the evidence is saved. Do not promote the 42-gap SOW, customer finance/UAT, container deployment, production identity, security, residency/recovery/availability or tax/e-invoicing gates to accepted without their required evidence and authorized human sign-off. Keep the original installation recoverable; laptop cleanup is a separate request.
```
