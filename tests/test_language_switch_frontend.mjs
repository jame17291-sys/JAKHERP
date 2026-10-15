/** Behavior checks for the actual switch; native guard/RTL also need live UI QA. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../addons/jakh_branding/static/src/language_switch.js', import.meta.url), 'utf8');
async function fixture({ lang = 'en_US', installed = [['en_US', 'English'], ['ar_001', 'Arabic']], allowed = true, failure = false } = {}) {
    const events = [];
    const starts = [];
    const user = { userId: 17, context: { lang }, updateContext(value) { events.push(['context', value]); Object.assign(this.context, value); } };
    const orm = { async write(...args) { events.push(['write', ...args]); if (failure) throw new Error('Cannot save'); } };
    const context = vm.createContext({
        Component: class { env = { bus: {} }; }, proxy: (value) => value,
        onWillStart: (fn) => starts.push(fn), usePlugin: () => orm, ORM: {},
        user, loadLanguages: async () => installed,
        clearUncommittedChanges: async () => { events.push(['guard']); return allowed; },
        browser: { location: { reload() { events.push(['reload']); } } },
        registry: { category: () => ({ add() {} }) },
    });
    vm.runInContext(source.replace(/^import .*;\n/gm, '').replace('export class', 'class') + '\nglobalThis.Switch = JakhLanguageSwitch;', context);
    const button = new context.Switch();
    button.setup();
    await Promise.all(starts.map((fn) => fn()));
    return { button, events, user };
}
const snapshot = (value) => JSON.parse(JSON.stringify(value));
let test = await fixture();
await test.button.switchLanguage();
assert.deepEqual(snapshot(test.events), [['guard'], ['write', 'res.users', [17], { lang: 'ar_001' }], ['context', { lang: 'ar_001' }], ['reload']]);
test = await fixture({ lang: 'ar_001' });
assert.equal(test.button.state.targetCode, 'en_US');
await test.button.switchLanguage();
assert.equal(test.user.context.lang, 'en_US');
test = await fixture({ allowed: false });
await test.button.switchLanguage();
assert.deepEqual(snapshot(test.events), [['guard']]);
assert.equal(test.user.context.lang, 'en_US');
assert.equal(test.button.state.busy, false);
test = await fixture({ failure: true });
await assert.rejects(test.button.switchLanguage(), /Cannot save/);
assert.deepEqual(snapshot(test.events).map((event) => event[0]), ['guard', 'write']);
assert.equal(test.user.context.lang, 'en_US');
assert.equal(test.button.state.busy, false);
test = await fixture();
await Promise.all([test.button.switchLanguage(), test.button.switchLanguage()]);
assert.equal(test.events.filter(([type]) => type === 'write').length, 1);
test = await fixture({ installed: [['en_US', 'English']] });
await test.button.switchLanguage();
assert.equal(test.button.state.targetCode, null);
assert.deepEqual(snapshot(test.events), []);
console.log('Language switch: 13 assertions passed (guard, own preference, round trip, failure, double click, installed languages).');
