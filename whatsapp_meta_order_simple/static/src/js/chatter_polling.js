/**
 * Polling del chatter para sale.order (Odoo 15)
 *
 * Detecta mensajes nuevos (ej: del webhook de WhatsApp) y refresca la vista
 * sin recargar la página.
 */

/** @odoo-module */

import { patch } from "@web/core/utils/patch";
import { FormController } from "@web/views/form/form_controller";
import { useService } from "@web/core/utils/hooks";

const POLL_INTERVAL_MS = 4000;

patch(FormController.prototype, "chatterPolling", {
    setup() {
        this._super(...arguments);
        const resModel = this.props.resModel;
        if (resModel === "sale.order") {
            this.orm = useService("orm");
            this._chatterLastCount = null;
            this._chatterPollTimer = null;
            this._chatterResModel = "sale.order";
        }
    },

    mounted() {
        this._super(...arguments);
        if (this._chatterResModel === "sale.order") {
            this._chatterStartPolling();
        }
    },

    willUnmount() {
        if (this._chatterResModel === "sale.order") {
            this._chatterStopPolling();
        }
        this._super(...arguments);
    },

    async _chatterPollCheck() {
        if (this._chatterResModel !== "sale.order") return;
        const model = this.model || (this.env && this.env.model);
        if (!model) return;
        const root = model.root;
        if (!root) return;
        const resId = root.resId || (root.data && root.data.id);
        if (!resId || root.isNew) return;
        if (document.visibilityState !== "visible") return;
        try {
            const count = await this.orm.call(
                "sale.order",
                "get_chatter_message_count",
                [[resId]]
            );
            if (this._chatterLastCount !== null && count > this._chatterLastCount) {
                await model.load({ resId: resId });
            }
            this._chatterLastCount = count;
        } catch (e) {
            // ignorar
        }
    },

    _chatterStartPolling() {
        if (this._chatterResModel !== "sale.order") return;
        this._chatterStopPolling();
        const self = this;
        const doPoll = function () {
            const model = self.model || (self.env && self.env.model);
            const root = model && model.root;
            if (root && (root.resId || (root.data && root.data.id))) {
                self._chatterPollCheck();
            }
        };
        this._chatterPollTimer = setInterval(doPoll, POLL_INTERVAL_MS);
        setTimeout(doPoll, 1500);
    },

    _chatterStopPolling() {
        if (this._chatterPollTimer) {
            clearInterval(this._chatterPollTimer);
            this._chatterPollTimer = null;
        }
    },
});
