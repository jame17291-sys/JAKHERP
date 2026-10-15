# JAKH ERP

English/Arabic contracting ERP for UAE/KSA main contractors, built on the pinned
Odoo Community foundation. This repository is the maintained source handover;
it is not a copy of a laptop, a live hosting service or a production-compliance
certificate.

## Start here on another laptop

```sh
git clone https://github.com/jame17291-sys/JAKHERP.git
cd JAKHERP
```

Read [the relocation guide](deploy/RELOCATION.md) for the complete installation
and another-assistant resume prompt. All paths below are relative to this checkout.
No previous ChatGPT account or original laptop path is needed to read or maintain
the code. A new assistant must have access to this private repository and its
checkout; GitHub access does not itself create an ERP owner account.

The repository contains no database dumps, filestores, passwords, API keys,
invitation links, virtual environments or machine binaries. Existing demo changes
and uploaded files need a separate encrypted state transfer. **That private-hosting
transfer and destination restore have not yet been completed. Do not retire the
original installation until they pass.**

## Current connected workflow

Opportunity/tender → reviewed BOQ and estimate → approved award/handover →
contract/project/WBS budget → material and subcontract execution → measured
certificates → posted invoices and reconciled payments → final account/closeout.

| Maintained addon | Operational scope |
|---|---|
| `jakh_core` | Unified dashboard/module directory, company/project/WBS/cost-code structure, budgets, roles and business audit |
| `jakh_estimate` | CRM-linked estimating, reviewed multi-workbook BOQ intake, resource/direct/indirect pricing, versions, repeated buildings and idempotent handover |
| `jakh_operations` | Controlled requisitions/POs, material receipt/AP matching, stock issues/returns, subcontract SES/certificates, variations, owner billing, advances/retention, labour/plant facts, P6/Jisr intake, CVR/cash/CPI-SPI, scheduled in-app reports and guarded closeout |
| `jakh_workflow` | Approved frozen-estimate to contract/project/WBS handover |
| `jakh_assurance` | Controlled drawings/specifications/revisions, reviews, inspections, NCRs, incidents, permits, risks and corrective actions |
| `jakh_migration` | Reviewed CRM intake with source identity/currency/provenance; no legacy password or opening-balance import |
| `jakh_branding` | JAKH navigation/theme/help/demo guide, native English/Arabic switch/RTL, presentation cleanup and disabled external lead-generation guards |

Standard sales, purchasing, stock, CRM, project, accounting, search/list/form,
permissions and native translation behavior remain framework responsibilities.
The runtime profile retains required dependencies even when a provider feature
has no menu. Do not remove accounting, audit, security, migration or compatibility
code merely because it is not a visible module.

## Installation and future website hosting

Use [the relocation steps](deploy/RELOCATION.md#3-fresh-container-installation-on-the-new-laptop)
and [deployment reference](deploy/DEPLOYMENT.md). The container definitions obtain
the exact upstream source from its official URL and verify the checksum in
[vendor.lock.json](vendor.lock.json). The lean build includes the approved
operational dependency closure and Arabic/English, not the retired applications
or all uninstalled addons. Third-party licenses/provenance are retained.

Each customer needs its own database, filestore, runtime, private secrets and
backup. Legal entities within a customer use multi-company. Defaults bind only
to localhost. A public website needs a reviewed TLS reverse proxy, exact tenant
routing, identity/MFA, backups, monitoring and the acceptance gates below.
Do not use GitHub Pages as an ERP backend or enable the optional Keycloak
development profile as production SSO.

The original host did not have Docker: the image and restore-to-container path
remain unaccepted until actually built, run and tested on the chosen host. Do not
weaken pinned dependencies, isolation or enrollment to work around a failed build.
Native tooling supports macOS/Linux or WSL, not plain Windows Python. The native
staging work directory is now this checkout's `work/`; old staging metadata and
compiled binaries must not be copied as a portable environment.

## Demo and testing

[The English/Arabic walkthrough](deploy/demo-walkthrough.html) describes the
retained active eleven-villa scenario, completed reference project and editable
practice records. In a properly prepared/restored demo, open `/jakh/demo` or
Help & product information → Open the demo walkthrough.

`scripts/seed_construction_demo.py` seeds only a stopped, marked, enrolled synthetic
native staging customer through normal role-controlled actions. It is idempotent
and never resets practice changes. It is **not** a validated Docker seed command;
fresh Docker installation is empty. Live P6/Jisr/bank/email transports and demo
report schedules stay disabled unless separately authorized/configured.

The recorded engineering baseline is 193 native tests in each synthetic market
(immutable r5 Saudi runtime; maintained-source UAE fixture), plus bilingual
PDF/XLSX and a populated synthetic recovery rehearsal. Historical counts and
private evidence paths in [staging verification](deploy/staging-verification.json)
are not new-host, customer or production acceptance.

Repository checks run publication safety, standard-library structural checks and
frontend regressions. The complete developer test suite additionally requires
the native dependencies, reviewed offline RTLCSS installation and pinned source:

```sh
python3.12 -m unittest discover -s tests -p 'test_*.py'
node tests/test_navigation_frontend.mjs
node tests/test_dashboard_frontend.mjs
node tests/test_language_switch_frontend.mjs
node tests/test_routes_frontend.mjs
node tests/test_crm_guidance_frontend.mjs
```

Install upstream requirements and the hashed `deploy/requirements-jakh.lock.txt`
in an isolated environment. Set `JAKH_NATIVE_SOURCE` to the verified source and
`JAKH_RTLCSS` to the reviewed compiler executable for actual RTL tests. Missing
optional tools produce skips, not acceptance. Native database-wide tests belong
in a clean isolated acceptance target, never over retained practice/customer data.

Before publishing, stage the exact intended files and run:

```sh
python3.12 -B scripts/verify_publish_tree.py --root . --json
git diff --cached --stat
```

The checker is a high-confidence safeguard, not an exhaustive security audit.
It reports paths/categories rather than credential contents. It does not rewrite
or cleanse Git history. Never add runtime state, secret files or a workspace ZIP.

## Delivery and acceptance records

- [15-step contracting completion register](requirements/construction-completion-15.json)
- [42-gap SOW acceptance matrix](requirements/acceptance-matrix.json)
- [Requirements traceability](requirements/traceability.json)
- [Runtime/dependency profile](deploy/runtime-modules.json)
- [Recorded engineering verification](deploy/staging-verification.json)
- [Data relocation and another-assistant handover](deploy/RELOCATION.md)

Live vendor transport, external scheduled delivery, approved FX/secondary-UOM
transaction workflows, statutory indirect cash flow/consolidation, wider SOW
assets/WIP and customer-specific cases remain separately governed. Production
SSO/MFA/control-plane, independent security review, finance UAT, two mock
migrations and reconciled production load, UAE/KSA tax/e-invoicing onboarding,
residency, monitored availability and measured recovery are not signed off.

The historical synthetic restore reconciled 30 balanced posted journals and
168 attachments. It does not establish production RPO/RTO or authorize deletion
of the original data. Code rollback does not roll back a database.

## License

JAKH addons are LGPL-3; see [LICENSE](LICENSE) and [COPYRIGHT](COPYRIGHT).
Upstream Odoo Community and bundled compiler assets retain their original
licenses and authorship. No Enterprise subscription/source is included.
