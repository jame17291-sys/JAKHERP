/**
 * Dependency-free browser-boundary regression tests against the real addon JS.
 * Run: node outputs/jakh-erp-odoo/tests/test_routes_frontend.mjs
 * Native authentication/action semantics are covered by the addon HTTP tests;
 * browser services below are spies, not replacements for those native tests.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const sourcePath = fileURLToPath(new URL('../addons/jakh_branding/static/src/routes.js', import.meta.url));
const source = fs.readFileSync(sourcePath, 'utf8');
const location = new URL('http://127.0.0.1:8810/jakh/settings?debug=1');
const actions = [];
const historyWrites = [];
const listeners = [];
let checks = 0;
const equal = (actual, expected, label) => {
    assert.equal(actual, expected, label);
    checks++;
};
const deepEqual = (actual, expected, label) => {
    assert.deepEqual(JSON.parse(JSON.stringify(actual)), expected, label);
    checks++;
};

function patch(target, extension) {
    // Preserve JavaScript super semantics for the addon's documented patches.
    const previous = Object.create(Object.getPrototypeOf(target));
    Object.defineProperties(previous, Object.getOwnPropertyDescriptors(target));
    Object.setPrototypeOf(extension, previous);
    Object.defineProperties(target, Object.getOwnPropertyDescriptors(extension));
}

class WebClient {
    constructor() {
        this.actionService = {
            loadState(state) {
                actions.push({ kind: 'loadState', state });
                return Promise.resolve(true);
            },
        };
        this.menuService = {
            getMenu(id) {
                return id === 42 ? { id, actionID: 604 } : null;
            },
            selectMenu(menu) {
                actions.push({ kind: 'selectMenu', menu });
                return Promise.resolve();
            },
        };
    }
    setup() {
        this.nativeSetupCalls = (this.nativeSetupCalls || 0) + 1;
    }
    onGlobalClick() {}
    _loadDefaultApp() {
        actions.push({ kind: 'defaultApp' });
    }
}
const nativeCaptureHandler = WebClient.prototype.onGlobalClick;
class NavBar {
    getMenuItemHref() {
        return '/odoo/sales';
    }
}

const router = {
    stateToUrl(state) {
        return `/odoo${state.action ? '/' + state.action : ''}`;
    },
    urlToState(url) {
        const state = Object.fromEntries(url.searchParams);
        if (url.pathname === '/web') {
            const hash = new URLSearchParams(url.hash.slice(1));
            if (hash.has('action')) state.action = Number(hash.get('action'));
            if (hash.has('id')) state.resId = Number(hash.get('id'));
        } else {
            const action = url.pathname.split('/')[2];
            if (action) state.action = action;
        }
        return state;
    },
};
const commandProvider = {
    provide() {
        return [{ href: '/odoo/sales', action: 'preserved' }];
    },
};
const context = vm.createContext({
    URL, DOMException, console, location, WebClient, NavBar, router, patch,
    window: {},
    document: { title: 'JAKH ERP' },
    navigator: {},
    browser: {
        navigator: {},
        history: {
            pushState(...args) { historyWrites.push(args); },
            replaceState(...args) { historyWrites.push(args); },
        },
    },
    rpcBus: { trigger() {} },
    registry: { category() { return { get() { return commandProvider; } }; } },
    startRouter() {},
    useListener(...args) { listeners.push(args); },
});

// Evaluate the production extension with only import bindings mocked. Do not
// copy the eligibility handler or fragment helper into the regression test.
vm.runInContext(source.replace(/^import\s.*;\s*$/gm, '').replace(/^export\s+/gm, ''), context, {
    filename: sourcePath,
});
const client = new WebClient();
client.setup();
equal(client.nativeSetupCalls, 1, 'native setup runs exactly once');
equal(WebClient.prototype.onGlobalClick, nativeCaptureHandler, 'native capture handler is unchanged');
equal(listeners.length, 1, 'one product route listener is installed');
equal(listeners[0][1], 'click', 'listener handles click');
equal(listeners[0][3], undefined, 'listener uses bubbling, not capture');

function click(href, options = {}) {
    const anchor = options.noAnchor ? null : {
        href: new URL(href || '', location.href).href,
        target: options.target || '',
        getAttribute(name) { return name === 'href' ? href : null; },
        hasAttribute(name) { return name === 'download' && Boolean(options.download); },
    };
    let prevented = 0;
    const event = {
        defaultPrevented: Boolean(options.defaultPrevented),
        ctrlKey: false, metaKey: false, shiftKey: false, altKey: false, button: 0,
        ...options,
        target: {
            closest(selector) {
                if (selector === '[contenteditable]') return options.editable ? {} : null;
                return anchor;
            },
        },
        preventDefault() { prevented++; this.defaultPrevented = true; },
    };
    actions.length = 0;
    client.onJakhRouteClick(event);
    return { prevented, actions: JSON.parse(JSON.stringify(actions)) };
}

for (const href of [
    '#crm', '#sales', '#', ' #crm', '',
    '/jakh/settings?debug=1#crm',
    'http://127.0.0.1:8810/jakh/settings?debug=1#crm',
]) {
    deepEqual(click(href), { prevented: 0, actions: [] }, `local tab/fragment is untouched: ${href}`);
}
for (const options of [
    { defaultPrevented: true }, { ctrlKey: true }, { metaKey: true },
    { shiftKey: true }, { altKey: true }, { button: 1 }, { editable: true },
    { target: '_blank' }, { download: true }, { noAnchor: true },
]) {
    deepEqual(click('/jakh/sales', options), { prevented: 0, actions: [] }, 'handled/modified/non-action click is untouched');
}
for (const href of [
    'https://other.example/jakh/sales', '/web/dataset/call_kw', '/jakh/help', '/jakh/demo',
    '/jakh/brand/icon/32.png', '/jakh/service-worker.js', '/jakh/offline',
]) {
    deepEqual(click(href), { prevented: 0, actions: [] }, `non-backend link is untouched: ${href}`);
}
for (const href of ['/jakh/sales', '/odoo/sales']) {
    deepEqual(click(href), {
        prevented: 1,
        actions: [{ kind: 'loadState', state: { action: 'sales' } }],
    }, `eligible link uses native state loading: ${href}`);
}
deepEqual(click('/web#action=604&id=11'), {
    prevented: 1,
    actions: [{ kind: 'loadState', state: { action: 604, resId: 11 } }],
}, 'legacy backend hash path still uses native parsing');
deepEqual(click('/jakh/settings?debug=2#crm'), {
    prevented: 1,
    actions: [{ kind: 'loadState', state: { debug: '2', action: 'settings' } }],
}, 'different query is not mistaken for a local fragment');
deepEqual(click('/jakh?menu_id=42'), {
    prevented: 1,
    actions: [{ kind: 'selectMenu', menu: { id: 42, actionID: 604 } }],
}, 'menu links use native filtered menu selection');
deepEqual(click('/jakh'), { prevented: 1, actions: [{ kind: 'defaultApp' }] }, 'home uses native default app');
equal(historyWrites.length, 0, 'route interception never mutates history before native validation');
equal(new NavBar().getMenuItemHref(), '/jakh/sales', 'navbar href is canonical');
equal(commandProvider.provide()[0].href, '/jakh/sales', 'search command href is canonical');
equal(commandProvider.provide()[0].action, 'preserved', 'native search command action is preserved');
location.href = 'http://127.0.0.1:8810/web/login';
deepEqual(click('/jakh/sales'), { prevented: 0, actions: [] }, 'non-webclient current routes are untouched');

console.log(`Route frontend regression: ${checks} assertions passed (Settings fragments, native handlers, modifiers, canonical/legacy action delegation, and history boundary).`);
