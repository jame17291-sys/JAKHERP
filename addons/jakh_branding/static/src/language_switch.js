/** @odoo-module **/

import { Component, onWillStart, proxy, usePlugin } from "@odoo/owl";
import { browser } from "@web/core/browser/browser";
import { loadLanguages } from "@web/core/l10n/translation";
import { ORM } from "@web/core/orm_plugin";
import { registry } from "@web/core/registry";
import { user } from "@web/core/user";
import { clearUncommittedChanges } from "@web/webclient/actions/action_plugin";

/** A personal preference, using native permissions and the native form guard. */
export class JakhLanguageSwitch extends Component {
    static template = "jakh_branding.LanguageSwitch";

    setup() {
        this.orm = usePlugin(ORM);
        this.state = proxy({ busy: false, targetCode: null });
        this.isArabic = user.context.lang?.startsWith("ar");
        onWillStart(async () => {
            const installed = await loadLanguages(this.orm);
            const codes = installed.map(([code]) => code);
            this.state.targetCode = this.isArabic
                ? (codes.includes("en_US") ? "en_US" : null)
                : (codes.includes("ar_001") ? "ar_001" : codes.find((code) => code.startsWith("ar_")) || null);
        });
    }

    async switchLanguage() {
        if (this.state.busy || !this.state.targetCode) return;
        this.state.busy = true;
        try {
            // Save/discard/validation follow the same path as native navigation.
            // Never write the preference or reload if leaving the form is blocked.
            if (!(await clearUncommittedChanges(this.env.bus))) return;
            await this.orm.write("res.users", [user.userId], { lang: this.state.targetCode });
            user.updateContext({ lang: this.state.targetCode });
            // Keep the current action, record, company and URL parameters. A full
            // reload loads translated assets and the correct text direction.
            browser.location.reload();
        } finally {
            this.state.busy = false;
        }
    }
}

registry.category("systray").add("jakh_branding.language_switch", {
    Component: JakhLanguageSwitch,
}, { sequence: 2 });
