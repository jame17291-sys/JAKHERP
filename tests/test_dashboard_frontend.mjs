/** Exercise actual dashboard drill-down with native-boundary spies. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync(new URL('../addons/jakh_core/static/src/dashboard.js', import.meta.url), 'utf8');
let opened;
const context = vm.createContext({
    Component: class {}, proxy: (value) => value, useProps: () => ({}), standardActionServiceProps: {},
    onWillStart() {}, registry: { category: () => ({ add() {} }) },
    user: { context: { uid: 17, lang: 'en_US' } }, _t: (value) => value,
    makeContext: (values) => Object.assign({}, ...values),
    rpc: async () => ({ name: 'Invoices', help: '<p>Sample guidance</p>', domain: [], useSampleModel: true,
        context: { search_default_draft: 1, default_move_type: 'out_invoice' } }),
});
vm.runInContext(source.replace(/^import .*;\n/gm, '').replace('export class', 'class') + '\nglobalThis.Dashboard = JakhDashboard;', context);
const dashboard = new context.Dashboard();
dashboard.action = { doAction: async (action) => { opened = action; } };
const domain = [['state', '=', 'posted'], ['company_id', 'in', [1]], ['move_type', '=', 'out_invoice']];
await dashboard.openCard({ action: 'account.action_move_out_invoice_type', domain, label: 'Overdue customer invoices' });
assert.equal(opened.useSampleModel, false);
assert.equal(opened.name, 'Overdue customer invoices');
assert.equal(opened.domain, domain);
assert.equal(opened.context.search_disable_custom_filters, true);
assert.equal('search_default_draft' in opened.context, false);
assert.equal(opened.context.default_move_type, 'out_invoice');
assert.equal(opened.help, 'No records match this indicator for your selected companies.');
assert.match(dashboard.formatAmount(250, 'AED'), /250/);
assert.equal(dashboard.formatIndex(null, true), 'Undefined');
assert.equal(dashboard.formatIndex(60, true), '60%');
assert.equal(dashboard.formatIndex(0.375), '0.375');
await dashboard.openConstruction({ id: 81 }, { company_id: 4 });
assert.equal(opened.res_model, 'jakh.construction.report');
assert.equal(opened.context.default_project_id, 81);
assert.deepEqual(Array.from(opened.context.allowed_company_ids), [4]);
assert.equal(opened.context.lang, 'en_US');
context.user.context.lang = 'ar_001';
assert.match(dashboard.formatAmount(250, 'AED'), /د\.إ\./);
assert.equal(dashboard.formatIndex(null), 'Undefined');
console.log('Dashboard drill-down and project control: 17 assertions passed (exact scope, native actions, English/Arabic amounts, undefined indices).');
