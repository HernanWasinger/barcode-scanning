# -*- coding: utf-8 -*-
"""
Extensión del modelo sale.order para integración con WhatsApp Meta.

Añade:
- Estado de conversación WhatsApp (abierta/cerrada)
- Sincronización chatter -> WhatsApp cuando la conversación está abierta
- Acciones para enviar template, cerrar conversación
- Método get_chatter_message_count para polling del chatter
"""

import logging
import re

from odoo import _, fields, models
from odoo.exceptions import UserError
# html2plaintext: convierte HTML del chatter a texto plano para enviar por WhatsApp
from odoo.tools import html2plaintext

_logger = logging.getLogger(__name__)
# Templates reservados por Meta para números de prueba (hello_world)
TEST_TEMPLATE_NAMES = {"hello_world"}


class SaleOrder(models.Model):
    """Extiende orden de venta para disparar envio por WhatsApp Meta."""

    # Hereda el modelo estándar de pedidos de venta
    _inherit = "sale.order"

    # Estado de la conversación WhatsApp vinculada a esta orden
    # "open" = ventana 24h activa, se puede enviar texto libre desde chatter
    # "closed" = ventana cerrada, hay que enviar template para reabrir
    whatsapp_conversation_state = fields.Selection(
        [("closed", "Cerrada"), ("open", "Abierta")],
        default="closed",
        string="Conversacion WhatsApp",
        copy=False,  # No se copia al duplicar pedido
        tracking=True,  # Aparece en chatter cuando cambia
    )

    def _is_whatsapp_conversation_open(self):
        """Indica si la conversación está abierta (ventana 24h activa)."""
        self.ensure_one()
        return self.whatsapp_conversation_state == "open"

    def message_post(self, **kwargs):
        """Sincroniza mensajes del chatter hacia WhatsApp cuando corresponde.

        Flujo:
        1. Si conversación abierta: suprime envío de emails (evita spam)
        2. Si el mensaje viene del chatter y es del usuario actual: intenta enviar a WhatsApp
        3. Ignora mensajes técnicos del propio módulo (ej: "Template WhatsApp enviado")
        """
        # Obtiene el subtipo del mensaje (comentario, nota, etc.)
        subtype_xmlid = kwargs.get("subtype_xmlid") or "mail.mt_comment"
        # Si la conversación está abierta, no enviamos emails (el cliente usa WhatsApp)
        suppress_email = bool(
            subtype_xmlid in ("mail.mt_comment", "mail.mt_note")
            and any(order._is_whatsapp_conversation_open() for order in self)
        )
        # Aplica contexto para suprimir emails si corresponde
        post_target = (
            self.with_context(mail_notify_noemail=True, no_email=True)
            if suppress_email
            else self
        )
        # Llama al message_post original (guarda el mensaje en chatter)
        message = super(SaleOrder, post_target).message_post(**kwargs)
        # Si viene de proceso interno (ej: webhook), no sincronizamos hacia fuera
        if self.env.context.get("whatsapp_skip_outbound_sync"):
            return message
        # Solo sincronizamos comentarios y notas (no otros subtipos)
        if subtype_xmlid not in ("mail.mt_comment", "mail.mt_note"):
            return message
        # Solo si el autor es el usuario actual (evita reenviar mensajes del cliente)
        if message.author_id != self.env.user.partner_id:
            return message
        # Extrae texto plano del body (puede venir HTML)
        body_html = kwargs.get("body") or message.body or ""
        plain_text = (html2plaintext(body_html) or "").strip()
        if not plain_text:
            return message
        # Evita reenviar notas técnicas del propio modulo
        technical_prefixes = (
            "Template WhatsApp enviado.",
            "Mensaje WhatsApp enviado al cliente.",
        )
        if plain_text.startswith(technical_prefixes):
            return message

        # Obtiene cuenta activa de WhatsApp
        account = self.env["whatsapp.meta.account"].get_default_account()
        for order in self:
            # Solo envía si la conversación está abierta
            if not order._is_whatsapp_conversation_open():
                continue
            try:
                try:
                    # Camino principal: enviar texto libre del chatter a WhatsApp
                    account.send_order_text(order, plain_text)
                except UserError as err:
                    err_msg = str(err)
                    # Si ventana 24h cerrada, informamos y no forzamos template
                    if any(
                        token in err_msg
                        for token in ("131047", "outside the allowed window", "requires a message template")
                    ):
                        raise UserError(
                            _(
                                "La ventana de 24h esta cerrada para la orden %s. "
                                "Volve a abrir la conversacion con 'Enviar WhatsApp'."
                            )
                            % order.name
                        )
                    raise
            except Exception:
                _logger.exception(
                    "No se pudo sincronizar chatter->WhatsApp para la orden %s", order.name
                )
                raise
        return message

    def get_chatter_message_count(self):
        """Cantidad de mensajes en el chatter. Usado por polling para auto-refresh.

        El JS chatter_polling.js llama este método cada 4 segundos para detectar
        mensajes nuevos (ej: del webhook) y refrescar la vista sin recargar.
        """
        self.ensure_one()
        return self.env["mail.message"].search_count(
            [
                ("model", "=", "sale.order"),
                ("res_id", "=", self.id),
            ]
        )

    def action_send_whatsapp_meta(self):
        """Boton de UI: abre wizard para elegir template y enviar."""
        for order in self:
            if order.state in ("cancel",):
                raise UserError(_("No se puede enviar WhatsApp en pedidos cancelados."))
        account = self.env["whatsapp.meta.account"].get_default_account()
        # Detecta si el número es de prueba (permite hello_world)
        allow_test_templates = account._allow_test_templates()
        approved_templates = self.env["whatsapp.meta.template"].search(
            [("account_id", "=", account.id), ("status", "=", "APPROVED")]
            + (
                []
                if allow_test_templates
                else [("is_test_template", "=", False), ("name", "not in", list(TEST_TEMPLATE_NAMES))]
            ),
            limit=1,
        )
        if not approved_templates:
            raise UserError(
                _(
                    "No hay templates aprobados para la cuenta activa. Sincroniza templates y reintenta."
                )
            )
        return {
            "type": "ir.actions.act_window",
            "name": _("Enviar WhatsApp"),
            "res_model": "whatsapp.send.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_account_id": account.id,
                "default_template_id": approved_templates.id,
                "default_sale_order_ids": [(6, 0, self.ids)],
                "default_allow_test_templates": allow_test_templates,
            },
        }

    def action_close_whatsapp_conversation(self):
        """Cierra conversacion para una orden y envia mensaje de cierre."""
        account = self.env["whatsapp.meta.account"].get_default_account()
        for order in self:
            if order.whatsapp_conversation_state == "closed":
                continue
            closing_text = _("Cerrando conversacion por orden %s") % (order.name or "-")
            try:
                # Intenta enviar texto libre primero
                account.send_order_text(order, closing_text)
            except UserError as err:
                err_msg = str(err)
                # Si ventana 24h cerrada, usa template de cierre
                if any(
                    token in err_msg
                    for token in ("131047", "outside the allowed window", "requires a message template")
                ):
                    account.send_order_template(order, conversation_event="close")
                else:
                    raise
            # Registra en chatter que se cerró
            order.with_context(whatsapp_skip_outbound_sync=True).message_post(
                body=_("Conversacion WhatsApp cerrada para la orden %s.") % (order.name or "-"),
                message_type="comment",
                subtype_xmlid="mail.mt_note",
            )
            order.whatsapp_conversation_state = "closed"
        return True

    def action_send_order_summary_whatsapp(self):
        """Envía por WhatsApp el resumen del pedido ya armado (solo si la conversación está abierta)."""
        self.ensure_one()
        if self.state == "cancel":
            raise UserError(_("No se puede enviar WhatsApp en pedidos cancelados."))
        if not self._is_whatsapp_conversation_open():
            raise UserError(
                _(
                    "La conversación de WhatsApp está cerrada. Usá 'Enviar WhatsApp' para abrirla "
                    "y después podés usar este botón para enviar el resumen del pedido."
                )
            )
        account = self.env["whatsapp.meta.account"].get_default_account()
        summary_text = account._get_order_summary_plain_text(self)
        parts = re.split(r"\s*Notas:\s*", summary_text, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) > 1:
            summary_text = parts[0]
        summary_text = re.sub(r"\n\[\d+\]\s*https?://\S*", "", summary_text)
        summary_text = re.sub(r"\n\[\d+\]\s*$", "", summary_text, flags=re.MULTILINE)
        summary_text = re.sub(r"\n\s*https?://\S*", "", summary_text)
        summary_text = summary_text.strip()
        account.with_context(whatsapp_skip_outbound_sync=True).send_order_text(self, summary_text)
        self.with_context(whatsapp_skip_outbound_sync=True).message_post(
            body=_("Resumen del pedido enviado por WhatsApp:\n\n%s") % summary_text,
            message_type="comment",
            subtype_xmlid="mail.mt_note",
        )
        return True
