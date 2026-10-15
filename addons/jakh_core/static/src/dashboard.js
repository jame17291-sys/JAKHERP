import { Component, onWillStart, proxy, useProps } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { rpc } from "@web/core/network/rpc";
import { makeContext } from "@web/core/context";
import { user } from "@web/core/user";
import { _t } from "@web/core/l10n/translation";
import { standardActionServiceProps } from "@web/webclient/actions/action_plugin";
import { getFirstActionMenu, getModuleEntries } from "@jakh_core/navigation";

export class JakhDashboard extends Component {
    static template = "jakh_core.Dashboard";
    props = useProps(standardActionServiceProps);
    state = proxy({ loading: true, error: "", data: { cards: [], companies: [], portfolio: [] } });
    refreshRevision = 0;

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.menu = useService("menu");
        onWillStart(() => this.refresh());
    }

    get modules() {
        return getModuleEntries(this.menu)
            .filter((entry) => entry.xmlid !== "jakh_core.menu_dashboard")
            .map((entry) => ({
                ...entry,
                destination: getFirstActionMenu(entry),
                summary: (entry.childrenTree || []).map((child) => child.name).join(" · "),
            }))
            .filter((entry) => entry.destination);
    }

    async refresh() {
        const revision = ++this.refreshRevision;
        this.state.loading = true;
        this.state.error = "";
        try {
            const data = await this.orm.call("jakh.dashboard", "get_data", []);
            if (revision === this.refreshRevision) {
                this.state.data = data;
            }
        } catch (error) {
            if (revision === this.refreshRevision) {
                this.state.error = error.message || _t("Dashboard could not be loaded.");
            }
        } finally {
            if (revision === this.refreshRevision) {
                this.state.loading = false;
            }
        }
    }

    async openCard(card) {
        // Resolve the existing native action rather than recreating a second router.
        const result = await rpc("/web/action/load", { action_id: card.action });
        result.domain = card.domain;
        result.name = card.label;
        // Native sample rows are illustrative, not contributors to this KPI.
        result.useSampleModel = false;
        result.help = _t("No records match this indicator for your selected companies.");
        result.context = makeContext([result.context || {}], user.context);
        // A saved default filter must not silently narrow a KPI's contributing records.
        for (const key of Object.keys(result.context)) {
            if (key.startsWith("search_default_")) {
                delete result.context[key];
            }
        }
        result.context.search_disable_custom_filters = true;
        return this.action.doAction(result);
    }

    openModule(module) { return this.menu.selectMenu(module.destination); }

    formatAmount(amount, currency) {
        return new Intl.NumberFormat(user.context.lang?.replace("_", "-") || "en", {
            style: "currency", currency, maximumFractionDigits: 2,
        }).format(amount);
    }

    formatIndex(value, percent = false) {
        if (value === null || value === undefined) { return _t("Undefined"); }
        return new Intl.NumberFormat(user.context.lang?.replace("_", "-") || "en", {
            maximumFractionDigits: percent ? 2 : 4,
        }).format(value) + (percent ? "%" : "");
    }

    openConstruction(project, company) {
        return this.action.doAction({
            type: "ir.actions.act_window", name: _t("Construction control"),
            res_model: "jakh.construction.report", view_mode: "form", views: [[false, "form"]],
            context: { ...user.context, allowed_company_ids: [company.company_id], default_project_id: project.id },
        });
    }

    openProjectAlerts(company) {
        return this.openCard({ label: _t("Project exceptions"), action: "jakh_operations.action_project_threshold_alerts", domain: company.alert_domain });
    }
}

registry.category("actions").add("jakh_dashboard", JakhDashboard);
