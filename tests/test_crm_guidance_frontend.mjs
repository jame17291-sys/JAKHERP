/** Narrow guidance changes must not alter the tour's business actions. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../addons/jakh_branding/static/src/crm_guidance.js', import.meta.url), 'utf8');
const template = fs.readFileSync(new URL('../addons/jakh_branding/static/src/crm_guidance.xml', import.meta.url), 'utf8');
const untouched = { trigger: '.o_kanban_record', run: 'click', content: 'Open an opportunity.' };
const breadcrumb = {
    trigger: '.breadcrumb-item:not(.active):first',
    run: 'click .breadcrumb-item:not(.active):last',
    content: 'Original vendor guidance',
    tooltipPosition: 'bottom',
    isActive: ['manual'],
};
const otherBreadcrumb = { ...breadcrumb, run: 'another action' };
const steps = Object.freeze([Object.freeze(untouched), Object.freeze(breadcrumb), Object.freeze(otherBreadcrumb)]);
let seenArguments;
const tour = {
    url: '/jakh/crm',
    steps(...args) { seenArguments = args; return steps; },
};
const translations = [];
const context = vm.createContext({
    registry: { category(name) {
        assert.equal(name, 'web_tour.tours');
        return { get(key) { assert.equal(key, 'crm_tour'); return tour; } };
    } },
    // Model the native patch's super chain without loading a web client.
    patch(target, extension) {
        Object.setPrototypeOf(extension, { ...target });
        Object.defineProperties(target, Object.getOwnPropertyDescriptors(extension));
    },
    _t(text) { translations.push(text); return text; },
});
vm.runInContext(source.replace(/^import .*;\n/gm, ''), context);
const result = tour.steps('unchanged argument');
assert.deepEqual(seenArguments, ['unchanged argument']);
assert.equal(result.length, steps.length);
assert.equal(result[0], untouched);
assert.equal(result[2], otherBreadcrumb, 'A different breadcrumb action is not rewritten');
assert.equal(tour.url, '/jakh/crm');
assert.equal(result[1].trigger, breadcrumb.trigger);
assert.equal(result[1].run, breadcrumb.run);
assert.equal(result[1].tooltipPosition, breadcrumb.tooltipPosition);
assert.equal(result[1].isActive, breadcrumb.isActive);
assert.equal(breadcrumb.content, 'Original vendor guidance', 'Native step objects are not mutated');
assert.match(result[1].content, /Resolve any validation messages or unsaved changes/);
assert.doesNotMatch(result[1].content, /Odoo|save all|automatically/i);
assert.equal(translations.length, 1, 'Only the target instruction is translated again');
tour.steps();
assert.equal(translations.length, 2, 'Translation is resolved when the tour is used');
assert.match(source, /import "@crm\/js\/tours\/crm";/, 'Native registration loads before the patch');
assert.match(template, /t-inherit="crm\.PlsTooltip"/);
assert.match(template, /t-if="this\.props\.probability === 0\.0"/);
assert.match(template, /<attribute name="t-if">this\.props\.probability !== 0\.0<\/attribute>/);
assert.doesNotMatch(template, /position="replace"/, 'Computed tooltip content is preserved rather than rebuilt');
assert.match(template, /Probability insights are not available yet\./);
assert.match(template, /Record won and lost opportunities/);
assert.doesNotMatch(source, /prepare_pls_tooltip_data|orm\.call|\.write\(/, 'Guidance never changes probability records');
console.log('CRM guidance: tour actions preserved; neutral text and zero-data-only tooltip guard passed.');
