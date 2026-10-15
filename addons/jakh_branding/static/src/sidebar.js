/** @odoo-module **/

import { Component, onMounted, onPatched, onWillDestroy, proxy, signal, useEffect, useListener } from "@odoo/owl";
import { browser } from "@web/core/browser/browser";
import { router, routerBus } from "@web/core/browser/router";
import { useBus, useService } from "@web/core/utils/hooks";
import { patch } from "@web/core/utils/patch";
import { WebClient } from "@web/webclient/webclient";
import { _t } from "@web/core/l10n/translation";
import { getModuleEntries, hasUniqueBranchAction } from "@jakh_core/navigation";
const COLLAPSED_STORAGE_KEY = "jakh.sidebar.collapsed.v1";

/** A native-menu presentation: no additional actions, routing or access grants. */
export class JakhSidebar extends Component {
    static template = "jakh_branding.Sidebar";
    root = signal.ref();
    mobileToggle = signal.ref();

    setup() {
        this.menuService = useService("menu");
        this.actionService = useService("action");
        this.mediaQuery = window.matchMedia("(max-width: 767px)");
        let collapsed = false;
        try {
            collapsed = browser.localStorage.getItem(COLLAPSED_STORAGE_KEY) === "true";
        } catch {
            // Private browsing/storage restrictions must not prevent navigation.
        }
        this.state = proxy({
            activeMenuId: null,
            expanded: {},
            collapsed,
            mobileOpen: false,
            isMobile: this.mediaQuery.matches,
            navigating: false,
            revision: 0,
        });
        this.focusDrawerOnPatch = false;
        this.navigationRendered = false;
        this.syncSelection();
        useBus(this.env.bus, "MENUS:APP-CHANGED", () => this.syncSelection());
        useBus(this.env.bus, "ACTION_MANAGER:UI-UPDATED", () => {
            if (this.state.navigating) {
                this.navigationRendered = true;
                if (this.state.isMobile) {
                    this.closeMobile(false);
                }
            }
            this.syncSelection();
        });
        useBus(routerBus, "ROUTE_CHANGE", () => this.syncSelection());
        useListener(this.mediaQuery, "change", () => {
            this.state.isMobile = this.mediaQuery.matches;
            if (!this.state.isMobile) {
                this.state.mobileOpen = false;
            }
        });
        useListener(window, "keydown", (event) => this.onWindowKeydown(event));
        useEffect(() => {
            document.body.classList.add("jakh-sidebar-enabled");
            document.body.classList.toggle("jakh-sidebar-collapsed", this.state.collapsed);
            document.body.classList.toggle("jakh-sidebar-mobile-open", this.state.isMobile && this.state.mobileOpen);
        });
        onMounted(() => this.syncSelection());
        onPatched(() => {
            if (this.focusDrawerOnPatch && this.state.mobileOpen) {
                this.focusDrawerOnPatch = false;
                this.root()?.querySelector("button, a[href]")?.focus();
            }
        });
        onWillDestroy(() => {
            document.body.classList.remove("jakh-sidebar-enabled", "jakh-sidebar-collapsed", "jakh-sidebar-mobile-open");
        });
    }

    get entries() {
        this.state.revision;
        return getModuleEntries(this.menuService);
    }

    get toggleLabel() {
        if (this.state.isMobile) return _t("Close navigation");
        return this.state.collapsed ? _t("Expand navigation") : _t("Collapse navigation");
    }

    findPath(menuId, entries = this.entries, ancestors = []) {
        for (const menu of entries) {
            const path = [...ancestors, menu.id];
            if (menu.id === menuId) {
                return path;
            }
            const found = this.findPath(menuId, menu.childrenTree || [], path);
            if (found.length) {
                return found;
            }
        }
        return [];
    }

    syncSelection() {
        this.state.revision += 1;
        const action = this.actionService.currentController?.action;
        const firstRouteAction = router.current.actionStack?.[0]?.action;
        const matches = this.menuService.getAll().filter((menu) =>
            (action?.id && String(menu.actionID) === String(action.id)) ||
            (!action?.id && firstRouteAction && (menu.actionID === firstRouteAction || menu.actionPath === firstRouteAction))
        );
        // An app root can share its action with a visible child (notably the
        // Construction root and Dashboard). Highlight the rendered entry,
        // never an invisible app root. Retain the selected leaf when a native
        // action legitimately appears under more than one menu.
        const visibleMatches = matches.filter((menu) => this.findPath(menu.id).length > 0);
        const active = visibleMatches.find((menu) => menu.xmlid === "jakh_core.menu_dashboard") ||
            visibleMatches.find((menu) => menu.id === this.state.activeMenuId) ||
            visibleMatches.find((menu) => !this.menuService.getMenuAsTree(menu.id).childrenTree.length) ||
            visibleMatches[0];
        if (!active) {
            this.state.activeMenuId = null;
            return;
        }
        if (this.state.activeMenuId !== active.id) {
            this.state.activeMenuId = active.id;
            for (const ancestor of this.findPath(active.id).slice(0, -1)) {
                this.state.expanded[ancestor] = true;
            }
        }
    }

    isActive(menu) {
        return this.state.activeMenuId === menu.id;
    }

    isAncestor(menu) {
        return !this.isActive(menu) && this.findPath(this.state.activeMenuId, [menu]).length > 0;
    }

    isExpanded(menu) {
        return Boolean(this.state.expanded[menu.id]);
    }

    hasUniqueBranchAction(menu) {
        return hasUniqueBranchAction(menu, this.entries);
    }

    iconFor(menu) {
        const xmlid = menu.xmlid || "";
        if (xmlid === "jakh_core.menu_dashboard") return "apps";
        if (xmlid === "jakh_core.menu_reports") return "bar_chart";
        if (xmlid === "jakh_core.menu_activities") return "task_alt";
        if (xmlid === "jakh_core.menu_administration") return "settings";
        if (xmlid === "jakh_core.menu_assurance") return "fact_check";
        if (xmlid.startsWith("crm.")) return "handshake";
        if (xmlid.startsWith("project.")) return "assignment";
        if (xmlid.startsWith("purchase.")) return "shopping_cart";
        if (xmlid.startsWith("stock.")) return "inventory_2";
        if (xmlid.startsWith("account.")) return "account_balance";
        if (xmlid.startsWith("jakh_estimate.")) return "calculate";
        if (xmlid.startsWith("jakh_assurance.")) return "fact_check";
        return menu.childrenTree?.length ? "folder" : "description";
    }

    toggleBranch(menu) {
        if (this.state.collapsed && !this.state.isMobile) {
            this.setCollapsed(false);
        }
        this.state.expanded[menu.id] = !this.state.expanded[menu.id];
    }

    async selectMenu(menu) {
        if (!menu.actionID || this.state.navigating) {
            return;
        }
        const previousController = this.actionService.currentController;
        this.navigationRendered = false;
        this.state.navigating = true;
        try {
            // Native selectMenu invokes the native action service, including
            // CLEAR-UNCOMMITTED-CHANGES. Never substitute a direct URL/router.
            await this.menuService.selectMenu(menu);
            const controller = this.actionService.currentController;
            if (controller !== previousController || this.navigationRendered) {
                this.state.activeMenuId = menu.id;
                this.syncSelection();
                if (this.state.isMobile) {
                    this.closeMobile(false);
                }
            }
        } finally {
            this.state.navigating = false;
        }
    }

    setCollapsed(collapsed) {
        this.state.collapsed = collapsed;
        try {
            browser.localStorage.setItem(COLLAPSED_STORAGE_KEY, String(collapsed));
        } catch {
            // The current session still has a working collapse control.
        }
    }

    toggleSidebar() {
        if (this.state.isMobile) {
            this.closeMobile();
        } else {
            this.setCollapsed(!this.state.collapsed);
        }
    }

    openMobile() {
        this.state.mobileOpen = true;
        this.focusDrawerOnPatch = true;
    }

    closeMobile(restoreFocus = true) {
        this.state.mobileOpen = false;
        this.focusDrawerOnPatch = false;
        if (restoreFocus) {
            this.mobileToggle()?.focus();
        }
    }

    onWindowKeydown(event) {
        if (this.state.isMobile && this.state.mobileOpen && event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            this.closeMobile();
        }
    }

    onDrawerKeydown(event) {
        if (!this.state.isMobile || !this.state.mobileOpen || event.key !== "Tab") {
            return;
        }
        const focusable = [...this.root().querySelectorAll("button:not([disabled]), a[href]")]
            .filter((element) => element.getClientRects().length);
        const first = focusable[0];
        const last = focusable.at(-1);
        if (!first) return;
        if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
        }
    }
}

patch(WebClient, {
    components: { ...WebClient.components, JakhSidebar },
});
