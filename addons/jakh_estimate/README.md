# Construction estimate vertical slice

The addon uses native Odoo 20 CRM, list/form/search/kanban/activity, graph/pivot, quotation, project and chatter interfaces. It depends on the JAKH core company/cost-code/WBS models and role groups.

An estimator creates a BOQ, resource build-ups and optional per-building quantity overrides. Unlisted buildings retain the baseline quantity; an override replaces that quantity for one building. Item and header markup multiply. A saved version stores exact decimal strings, stable BOQ IDs, building basis, author and changes from its predecessor.

Submission locks edits while the manager reviews the study. Approval captures an immutable version. The manager can then create one native **draft quotation**, one project and draft WBS budgets. Repeated handover returns the existing quotation. Every quotation line is checked against the approved selling amount, so native price precision or price-included taxes cannot silently change that amount. Native quotation confirmation, signed-contract governance, finance configuration and WBS budget release are separate steps.

The first handover requires the company currency and a matching Sales pricelist. Original CRM references, JSON fields, amount and source currency remain intact on the opportunity. Foreign-currency studies can be retained and versioned, but Finance must prepare an explicitly converted company-currency study before handover. No exchange rate is inferred. Estimate analysis is scoped to the current company currency.

Server methods enforce editing roles, states, snapshot immutability and handover linkage; UI readonly flags are additional conveniences. Company domains use the Odoo 20 `ir.access` format. Financial fields and version payloads are restricted through `jakh_core.group_financial`; technical/quality roles can read the permitted quantity and descriptive fields.

Run dependency-free arithmetic tests with:

```sh
python3 addons/jakh_estimate/tests/test_costing_standalone.py
```

`tests/test_estimate.py` contains Odoo TransactionCase tests for approval locks, native/idempotent handover, quantity-override revision mapping, role restrictions and CRM currency preservation. These require a verified Odoo 20 installation and PostgreSQL; static XML/Python checks and standalone tests do not confirm module installation.
