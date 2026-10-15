# JAKH ERP maintenance handover

This is a standalone repository. Read README.md, deploy/RELOCATION.md,
deploy/DEPLOYMENT.md, vendor.lock.json and the requirements registers before
changing the implementation. The user wants a full contracting ERP, not a module
launcher: preserve linked documents, approvals, actual accounting and source traceability.

## Scope and structure

- Seven maintained addons live in addons/. Operational module/dependency closure
  is deploy/runtime-modules.json. Native framework source is acquired from the
  verified vendor.lock.json URL, not a path on the original laptop.
- Keep one unified dashboard/Modules column, native Arabic/English translation
  and RTL, and the established teal/white JAKH presentation.
- Preserve Odoo security/transactions, original licenses/notices, audit, migration
  provenance and private accounting integrity. Branding is not source authorship.
- Do not delete dependency code because it has no visible menu. Superseded
  translation fragments, caches and retired legacy applications are not the
  operational source of truth.

## State and authority

- GitHub contains code, not the running ERP, database, attachments, credentials or
  earlier chat context. Existing state transfer to private hosting is unfinished
  until an authorized destination and restore evidence exist.
- Never print, commit or ask for API keys, passwords, private invitations, tokens,
  encryption keys, customer dumps or filestores in chat/GitHub. Use approved secret
  files or a secret manager outside the checkout.
- One database/filestore/runtime per customer. Companies belong inside that
  customer. Cross-customer access must be explicit and audited.
- Do not overwrite a populated database, forge enrollment/staging/demo markers,
  remove volumes, reset practice data or delete the original laptop installation.
  A source rollback does not revert a database. Ask for exact destination/cutover
  authority when missing.
- P6 and Jisr remain specialist masters. Synthetic integrations must not contact
  live payroll, bank, email or vendor services.

## Verification

- scripts/verify_publish_tree.py checks staged/tracked publication candidates;
  run it after staging and again before push. Review Git history separately.
- Run the portable tests and frontend regressions documented in README.md.
  Actual RTL tests require verified native source and the bundled reviewed
  compiler; skips are not passing RTL evidence.
- Native database tests run only in a marked, stopped synthetic acceptance target,
  not over retained demo/customer data. Preserve users' practice changes.
- Container definitions remain unaccepted until built/run/export-tested on the
  selected host. init is empty-database only; it is not restored-data enrollment.
- Keep production/customer/SOW acceptance unsigned until its required evidence
  and authorized finance, security, identity, residency and localization approvals
  exist. Historical paths/counts in staging-verification.json are context only.
