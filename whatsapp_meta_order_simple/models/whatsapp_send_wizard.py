# -*- coding: utf-8 -*-
"""
Wizard para enviar template de WhatsApp desde una orden de venta.

Permite elegir el template y opcionalmente enviar sin parámetros.
Al confirmar, envía el template a todos los pedidos seleccionados
y marca la conversación como abierta.
"""

# api: decoradores @api.onchange, @api.depends
# fields: definición de campos
# models: base de modelos
from odoo import _, api, fields, models
from odoo.exceptions import UserError

# Templates reservados por Meta para números de prueba
TEST_TEMPLATE_NAMES = {"hello_world"}


class WhatsappSendWizard(models.TransientModel):
    """Wizard para elegir template al momento de enviar."""

    _name = "whatsapp.send.wizard"
    _description = "WhatsApp Send Wizard"

    # Cuenta de WhatsApp a usar (viene del contexto)
    account_id = fields.Many2one(
        "whatsapp.meta.account",
        string="Cuenta WhatsApp",
        required=True,
        readonly=True,
    )
    # Template seleccionado para enviar
    template_id = fields.Many2one(
        "whatsapp.meta.template",
        string="Template aprobado",
        required=True,
        domain="[('id', 'in', allowed_template_ids)]",
    )
    # Lista de templates permitidos (según si es número de prueba o no)
    allowed_template_ids = fields.Many2many(
        "whatsapp.meta.template",
        compute="_compute_allowed_template_ids",
    )
    # True si el número de la cuenta es de prueba (sandbox)
    allow_test_templates = fields.Boolean(
        string="Numero de prueba detectado",
        default=False,
        readonly=True,
    )
    # Si está activo, envía template sin variables (body vacío)
    send_without_parameters = fields.Boolean(
        string="Enviar sin parametros",
        help="Si esta activo, no se enviaran variables en el body del template.",
    )
    # Pedidos a los que se enviará el template
    sale_order_ids = fields.Many2many(
        "sale.order",
        string="Pedidos",
        readonly=True,
    )

    @api.onchange("template_id")
    def _onchange_template_id(self):
        """Al cambiar template: si no tiene parámetros, marca enviar sin parámetros."""
        for rec in self:
            rec.send_without_parameters = bool(
                rec.template_id and int(rec.template_id.body_params_count or 0) == 0
            )

    @api.depends("account_id", "allow_test_templates")
    def _compute_allowed_template_ids(self):
        """Calcula templates permitidos según cuenta y si es número de prueba."""
        for rec in self:
            domain = [("account_id", "=", rec.account_id.id), ("status", "=", "APPROVED")]
            # Si no es número de prueba, excluye hello_world y similares
            if not rec.allow_test_templates:
                domain += [("is_test_template", "=", False), ("name", "not in", list(TEST_TEMPLATE_NAMES))]
            rec.allowed_template_ids = self.env["whatsapp.meta.template"].search(domain)
            # Si el template actual ya no está permitido, lo limpia
            if rec.template_id and rec.template_id not in rec.allowed_template_ids:
                rec.template_id = False

    def action_confirm_send(self):
        """Envia el template elegido a todos los pedidos seleccionados."""
        self.ensure_one()
        if not self.sale_order_ids:
            raise UserError(_("No hay pedidos para enviar."))
        # Valida que no use template de prueba en número productivo
        if (
            not self.allow_test_templates
            and (
                self.template_id.is_test_template
                or (self.template_id.name or "").lower() in TEST_TEMPLATE_NAMES
            )
        ):
            raise UserError(
                _(
                    "El template '%s' es de prueba y solo puede usarse con numero de prueba."
                )
                % self.template_id.name
            )
        for order in self.sale_order_ids:
            if order.state in ("cancel",):
                raise UserError(
                    _("No se puede enviar WhatsApp en pedidos cancelados: %s") % order.name
                )
            # Envía template con o sin componentes según opción
            self.account_id.send_order_template(
                order,
                template=self.template_id,
                force_no_components=self.send_without_parameters,
            )
            # Marca conversación como abierta (ventana 24h activa)
            order.whatsapp_conversation_state = "open"
            # Registra en chatter que se inició la conversación
            order.with_context(whatsapp_skip_outbound_sync=True).message_post(
                body=_("Iniciando conversacion por orden de venta %s.") % (order.name or "-"),
                message_type="comment",
                subtype_xmlid="mail.mt_note",
            )
        return {"type": "ir.actions.act_window_close"}
