/** @odoo-module **/

import "@crm/js/tours/crm";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { patch } from "@web/core/utils/patch";

// Keep the native tour and every action intact. Replace only its breadcrumb
// instruction; navigation is not a guarantee that every change can be saved.
patch(registry.category("web_tour.tours").get("crm_tour"), {
    steps(...args) {
        return super.steps(...args).map((step) => {
            if (step.trigger !== ".breadcrumb-item:not(.active):first" ||
                step.run !== "click .breadcrumb-item:not(.active):last") {
                return step;
            }
            return {
                ...step,
                content: _t("Use the breadcrumbs to return to your pipeline. Resolve any validation messages or unsaved changes before leaving."),
            };
        });
    },
});
