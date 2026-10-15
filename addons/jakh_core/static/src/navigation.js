/** @odoo-module **/

/** One module list shared by the dashboard and sidebar, from native ACL-filtered menus. */
export function getModuleEntries(menuService) {
    const entries = [];
    const seen = new Set();
    for (const app of menuService.getApps()) {
        const tree = menuService.getMenuAsTree(app.id);
        // Only the product shell is flattened. Other installed application roots
        // remain reachable, including modules installed after initial setup.
        const modules = app.xmlid === "jakh_core.menu_construction" ? tree.childrenTree : [tree];
        for (const menu of modules || []) {
            if (!seen.has(menu.id)) {
                seen.add(menu.id);
                entries.push(menu);
            }
        }
    }
    return entries;
}

export function getFirstActionMenu(menu) {
    if (menu.actionID) return menu;
    for (const child of menu.childrenTree || []) {
        const actionMenu = getFirstActionMenu(child);
        if (actionMenu) return actionMenu;
    }
    return null;
}

/** Keep a branch shortcut only when its action needs an extra link.
 * Compare native action IDs, never translated labels or action paths: separate
 * actions may target the same model with different filters, views or contexts.
 * Only inspect the native permission-filtered tree, without mutating it.
 */
export function hasUniqueBranchAction(menu, entries = [menu]) {
    if (!menu.actionID || !menu.childrenTree?.length) return false;
    const actionID = String(menu.actionID);
    const containsAction = (children) => children.some((child) =>
        (child.actionID && String(child.actionID) === actionID) ||
        containsAction(child.childrenTree || [])
    );
    // A descendant action remains reachable as a leaf or its own unique branch
    // shortcut. Elsewhere, suppress only against an explicit leaf so two branch
    // shortcuts never suppress each other and make an action unreachable.
    const containsLeafAction = (menus) => menus.some((entry) =>
        entry.childrenTree?.length ? containsLeafAction(entry.childrenTree) :
            entry.actionID && String(entry.actionID) === actionID
    );
    return !containsAction(menu.childrenTree) && !containsLeafAction(entries);
}
