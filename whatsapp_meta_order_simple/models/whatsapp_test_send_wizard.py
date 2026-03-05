# -*- coding: utf-8 -*-
"""
Wizard para enviar mensajes de prueba de WhatsApp a un número puntual.

No está vinculado a una orden de venta; sirve para probar la conexión
y templates sin afectar pedidos reales.
"""

# api: decoradores @api.depends, @api.onchange
# fields: definición de campos
# models: base de modelos
from odoo import _, api, fields, models
from odoo.exceptions import UserError

# Templates reservados por Meta para números de prueba
TEST_TEMPLATE_NAMES = {"hello_world"}


class WhatsappTestSendWizard(models.TransientModel):
    """Wizard para enviar mensajes de prueba a un numero puntual."""

    _name = "whatsapp.test.send.wizard"
    _description = "WhatsApp Test Send Wizard"

    # Cuenta de WhatsApp (viene del contexto)
    account_id = fields.Many2one(
        "whatsapp.meta.account",
        string="Cuenta WhatsApp",
        required=True,
        readonly=True,
    )
    # Contacto opcional para autocompletar teléfono
    partner_id = fields.Many2one(
        "res.partner",
        string="Contacto",
        domain=[("type", "!=", "private")],  # Excluye contactos privados
    )
    # Número de destino (obligatorio)
    to_phone = fields.Char(string="Telefono destino", required=True)
    # True si el número de la cuenta es de prueba (sandbox)
    allow_test_templates = fields.Boolean(
        string="Numero de prueba detectado",
        default=False,
        readonly=True,
        help="Se detecta automaticamente desde la configuracion del numero en Meta.",
    )
    # Template a enviar (solo los que no requieren parámetros)
    template_id = fields.Many2one(
        "whatsapp.meta.template",
        string="Template aprobado",
        required=True,
        domain="[('id', 'in', allowed_template_ids)]",
    )
    # Lista de templates permitidos
    allowed_template_ids = fields.Many2many(
        "whatsapp.meta.template",
        compute="_compute_allowed_template_ids",
    )

    @api.depends("account_id", "allow_test_templates")
    def _compute_allowed_template_ids(self):
        """Calcula templates permitidos; excluye los que requieren parámetros."""
        for rec in self:
            domain = [("account_id", "=", rec.account_id.id), ("status", "=", "APPROVED")]
            if not rec.allow_test_templates:
                domain += [("is_test_template", "=", False), ("name", "not in", list(TEST_TEMPLATE_NAMES))]
            rec.allowed_template_ids = self.env["whatsapp.meta.template"].search(domain)
            if rec.template_id and rec.template_id not in rec.allowed_template_ids:
                rec.template_id = False

    @api.onchange("partner_id")
    def _onchange_partner_id_fill_phone(self):
        """Al seleccionar contacto: autocompleta teléfono desde mobile/phone."""
        for rec in self:
            if not rec.partner_id:
                continue
            phone = rec.partner_id.mobile or rec.partner_id.phone or rec.partner_id.phone_sanitized
            if phone:
                rec.to_phone = phone

    def action_send_test(self):
        """Envia template de prueba al numero ingresado en el wizard."""
        self.ensure_one()
        if not self.to_phone:
            raise UserError(_("Complete el telefono destino."))
        if not self.template_id:
            raise UserError(_("Seleccione un template aprobado."))
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
        # En prueba solo se permiten templates sin parámetros
        if int(self.template_id.body_params_count or 0) > 0:
            raise UserError(
                _(
                    "El template '%s' requiere parametros de body y no se puede enviar desde prueba rapida."
                )
                % self.template_id.name
            )
        # Construye payload y envía
        payload = self.account_id._build_template_payload(
            self.to_phone,
            template_name=self.template_id.name,
            language=self.template_id.language_code,
        )
        self.account_id._send_payload(payload, operation="send_test_template")
        return {"type": "ir.actions.act_window_close"}
