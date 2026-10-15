/** @odoo-module **/

/**
 * Replace vendor promotions in customer-facing presentation, not business
 * records, framework namespaces, validation messages, or license notices.
 */
import { patch } from "@web/core/utils/patch";
import { SettingsApp } from "@web/webclient/settings_form_view/settings/settings_app";
import { SettingsPage } from "@web/webclient/settings_form_view/settings/settings_page";

const SETTINGS_ICONS = Object.freeze({
    base: "settings",
    general_settings: "settings",
    crm: "handshake",
    sale: "assignment",
    sale_management: "assignment",
    account: "account_balance",
    purchase: "shopping_cart",
    stock: "inventory_2",
    project: "assignment",
    calendar: "calendar_today",
    mail: "mail",
    jakh_core: "construction",
    jakh_assurance: "fact_check",
});

export function jakhSettingsIcon(key) {
    return SETTINGS_ICONS[key] || "settings";
}

patch(SettingsPage.prototype, {
    jakhSettingsIcon(key) {
        return jakhSettingsIcon(key);
    },
});

patch(SettingsApp.prototype, {
    get jakhSettingsIcon() {
        return jakhSettingsIcon(this.props.key);
    },
});
