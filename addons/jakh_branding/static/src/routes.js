import { useListener } from "@odoo/owl";
import { browser, location } from "@web/core/browser/browser";
import { router, startRouter } from "@web/core/browser/router";
import { rpcBus } from "@web/core/network/rpc";
import { registry } from "@web/core/registry";
import { patch } from "@web/core/utils/patch";
import { WebClient } from "@web/webclient/webclient";
import { NavBar } from "@web/webclient/navbar/navbar";
import "@web/webclient/menus/menu_providers";

const isBackendPath = (path, prefix) => path === prefix || path.startsWith(`${prefix}/`);

/** Canonicalize an application path, not an API, asset namespace, or host. */
export function canonicalBackendUrl(value) {
    if (typeof value !== "string") {
        return value;
    }
    return value.replace(/^\/odoo(?=\/|\?|#|$)/, "/jakh");
}

/** Tabs and local section links are UI navigation, not new backend actions. */
export function isLocalFragmentNavigation(href, currentHref) {
    if (!href || href.trim().startsWith("#")) {
        return true;
    }
    const currentUrl = new URL(currentHref);
    const targetUrl = new URL(href, currentHref);
    return Boolean(targetUrl.hash) && targetUrl.origin === currentUrl.origin &&
        targetUrl.pathname === currentUrl.pathname && targetUrl.search === currentUrl.search;
}

patch(router, {
    stateToUrl(state) {
        return canonicalBackendUrl(super.stateToUrl(state));
    },
    urlToState(url) {
        const nativeUrl = new URL(url.href);
        if (isBackendPath(nativeUrl.pathname, "/jakh")) {
            nativeUrl.pathname = `/odoo${nativeUrl.pathname.slice(5)}`;
        }
        // Reuse native action/model/resId parsing and /web hash compatibility.
        const nextState = super.urlToState(nativeUrl);
        nativeUrl.pathname = canonicalBackendUrl(nativeUrl.pathname);
        url.href = nativeUrl.href;
        return nextState;
    },
});

// The native module initialized before this extension, so parse the canonical
// deep link again before the web client mounts and loads its first action.
startRouter();

if (browser.navigator.share) {
    router.shareUrl = async function () {
        const url = new URL(location.href);
        if (isBackendPath(url.pathname, "/scoped_app")) {
            url.pathname = `/jakh${url.pathname.slice(11)}`;
        } else {
            url.pathname = canonicalBackendUrl(url.pathname);
        }
        try {
            await browser.navigator.share({ url: url.href, title: document.title });
        } catch (error) {
            if (!(error instanceof DOMException && error.name === "AbortError")) {
                throw error;
            }
        }
    };
}

patch(NavBar.prototype, {
    getMenuItemHref() {
        return canonicalBackendUrl(super.getMenuItemHref(...arguments));
    },
});

patch(registry.category("command_provider").get("menu"), {
    provide() {
        return super.provide(...arguments).map((command) => ({
            ...command,
            href: canonicalBackendUrl(command.href),
        }));
    },
});

patch(WebClient.prototype, {
    setup() {
        super.setup(...arguments);
        // Leave the native capture-phase modified-click handler untouched.
        // Breadcrumbs and relational fields must get their target/bubbling
        // handlers first; only unhandled backend anchors reach this listener.
        useListener(window, "click", this.onJakhRouteClick.bind(this));
    },
    onJakhRouteClick(event) {
        if (
            event.defaultPrevented || event.ctrlKey || event.metaKey || event.shiftKey ||
            event.altKey || event.button !== 0 || event.target.closest("[contenteditable]")
        ) {
            return;
        }
        const anchor = event.target.closest("a[href]");
        if (!anchor || anchor.target === "_blank" || anchor.hasAttribute("download")) {
            return;
        }
        // Settings tabs use href="#crm". Leave their native tab/fragment
        // behavior intact instead of loading the current action from scratch.
        if (isLocalFragmentNavigation(anchor.getAttribute("href"), location.href)) {
            return;
        }
        const url = new URL(anchor.href, location.href);
        if (
            url.origin !== location.origin ||
            !isBackendPath(location.pathname, "/jakh") ||
            !(isBackendPath(url.pathname, "/jakh") || isBackendPath(url.pathname, "/odoo") || url.pathname === "/web")
        ) {
            return;
        }
        // Do not intercept product help, icon, worker, or future integration
        // routes beneath /jakh; only actual backend actions belong to the SPA.
        if (/^\/jakh\/(help|demo|brand|service-worker\.js|offline)(\/|$)/.test(url.pathname)) {
            return;
        }
        event.preventDefault();
        const nextState = router.urlToState(url);
        if (nextState.action || nextState.model || nextState.actionStack?.length) {
            // Explicit native state loading preserves server action access and
            // CLEAR-UNCOMMITTED-CHANGES. Do not change history before approval.
            this.actionService.loadState(nextState);
        } else if (nextState.menu_id) {
            const menu = this.menuService.getMenu(Number(nextState.menu_id));
            if (menu) {
                this.menuService.selectMenu(menu);
            }
        } else {
            this._loadDefaultApp();
        }
    },
    registerServiceWorker() {
        if (navigator.serviceWorker) {
            navigator.serviceWorker
                .register("/jakh/service-worker.js", { scope: "/jakh" })
                .then((registration) => {
                    if (registration.active && registration.active.state === "activated") {
                        this.serviceWorkerActivationResolvers.resolve();
                    } else {
                        const worker = registration.installing || registration.waiting || registration.active;
                        worker.addEventListener("statechange", (event) => {
                            if (event.target.state === "activated") {
                                this.serviceWorkerActivationResolvers.resolve();
                            }
                        });
                    }
                    navigator.serviceWorker.ready.then(() => {
                        if (!navigator.serviceWorker.controller) {
                            rpcBus.trigger("CLEAR-CACHES");
                        }
                    });
                })
                .catch((error) => {
                    console.error("Service worker registration failed, error:", error);
                });
        }
    },
});
