/** @odoo-module **/

/**
 * Product identity only: keep business controls, security exceptions and
 * diagnostic details intact. Upstream copyright and licensing remain in the
 * distributed source and the local open-source notices page.
 */
import "@web/webclient/user_menu/user_menu_items";
import "@mysubscription/user_menu";
import { t } from "@odoo/owl";
import { browser } from "@web/core/browser/browser";
import { Dialog } from "@web/core/dialog/dialog";
import {
    ClientErrorDialog,
    ErrorDialog,
    NetworkErrorDialog,
    RPCErrorDialog,
    RedirectWarningDialog,
    WarningDialog,
    odooExceptionTitleMap,
} from "@web/core/errors/error_dialogs";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { patch } from "@web/core/utils/patch";
import { UpgradeDialog } from "@web/webclient/settings_form_view/fields/upgrade_dialog";
import { DocumentationLink } from "@web/views/widgets/documentation_link/documentation_link";

const userMenu = registry.category("user_menuitems");

patch(DocumentationLink.prototype, {
    get url() {
        const original = super.url;
        if (/^https?:\/\/(?:www\.)?odoo\.com\/documentation\//.test(original)) {
            const topic = new URL(original).pathname.replace(/^\/documentation\/[^/]+/, "");
            return `/jakh/help?topic=${encodeURIComponent(topic)}`;
        }
        // Keep third-party API/security documentation destinations honest.
        return original;
    },
});

userMenu.add(
    "support",
    () => ({
        type: "item",
        id: "support",
        description: _t("JAKH Help"),
        href: "/jakh/help",
        callback: () => browser.open("/jakh/help", "_blank"),
        sequence: 20,
    }),
    { force: true }
);

// This item advertises the upstream vendor's plans; it is not JAKH customer
// provisioning or tenant administration. All administration controls remain.
userMenu.remove("mysubscription_user_menu");

// Odoo 20 uses a runtime props schema rather than static defaultProps.
patch(Dialog, {
    propsSchema: {
        ...Dialog.propsSchema,
        title: t.string().optional("JAKH ERP"),
    },
});

patch(ErrorDialog, { title: _t("JAKH ERP Error") });
patch(ClientErrorDialog, { title: _t("JAKH ERP Client Error") });
patch(NetworkErrorDialog, { title: _t("JAKH ERP Network Error") });

patch(RPCErrorDialog.prototype, {
    inferTitle() {
        super.inferTitle();
        // Access denied, validation and missing-record titles stay unchanged.
        if (this.props.exceptionName && odooExceptionTitleMap.has(this.props.exceptionName)) {
            return;
        }
        const titles = {
            server: _t("JAKH ERP Server Error"),
            script: _t("JAKH ERP Client Error"),
            network: _t("JAKH ERP Network Error"),
        };
        if (titles[this.props.type]) {
            this.title = titles[this.props.type];
        }
    },
});

patch(WarningDialog.prototype, {
    inferTitle() {
        if (
            !this.props.title &&
            !(this.props.exceptionName && odooExceptionTitleMap.has(this.props.exceptionName))
        ) {
            return _t("JAKH ERP Warning");
        }
        return super.inferTitle();
    },
});

patch(RedirectWarningDialog.prototype, {
    setup() {
        super.setup();
        if (!this.props.subType) {
            this.title = _t("JAKH ERP Warning");
        }
    },
});

patch(UpgradeDialog.prototype, {
    async _confirmUpgrade() {
        // No vendor promotion, user-count disclosure or claim that a missing
        // feature is enabled. The administrator can review JAKH's actual scope.
        browser.open("/jakh/help#optional-features", "_blank");
        this.props.close();
    },
});
