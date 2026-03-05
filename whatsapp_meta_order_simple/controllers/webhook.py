# -*- coding: utf-8 -*-
"""
Controlador HTTP para el webhook de WhatsApp Cloud API (Meta).

Expone el endpoint /whatsapp/meta/webhook que Meta llama para:
- GET: verificación inicial (handshake) con hub.verify_token y hub.challenge
- POST: eventos de mensajes entrantes (firmados con x-hub-signature-256)
"""

# json: para parsear el body del POST
import json
# logging: para registrar eventos y errores
import logging

# Controller: base para controladores HTTP
# request: acceso a request HTTP y env de Odoo
# route: decorador para definir rutas
from odoo.http import Controller, request, route

_logger = logging.getLogger(__name__)


class WhatsappMetaWebhookController(Controller):
    """Endpoint publico para handshake y eventos entrantes de Meta."""

    # Ruta pública (auth="public") sin CSRF (Meta no envía token CSRF)
    @route(
        "/whatsapp/meta/webhook",
        type="http",
        auth="public",
        methods=["GET", "POST"],
        csrf=False,
    )
    def whatsapp_meta_webhook(self, **kwargs):
        """Maneja:
        - GET: verificacion inicial del webhook (hub.challenge)
        - POST: eventos firmados por Meta (mensajes/estados)
        """
        if request.httprequest.method == "GET":
            # --- Handshake de Meta ---
            # Meta envía hub.verify_token para validar que somos nosotros
            verify_token = kwargs.get("hub.verify_token")
            # Meta espera que devolvamos hub.challenge en el body
            challenge = kwargs.get("hub.challenge")
            # Busca cuenta con ese verify_token
            account = request.env["whatsapp.meta.account"].sudo().get_account_by_verify_token(
                verify_token
            )
            if not account:
                return request.make_response("invalid verify token", status=403)
            # Devuelve el challenge para que Meta confirme la suscripción
            return request.make_response(challenge or "", status=200)

        # --- Flujo POST: eventos de mensajes ---
        # Obtiene body crudo (necesario para validar firma)
        raw_body = request.httprequest.get_data()
        # Meta firma el body con HMAC SHA256 en este header
        signature = request.httprequest.headers.get("x-hub-signature-256")
        # Busca cuenta activa
        account = request.env["whatsapp.meta.account"].sudo().search(
            [("active", "=", True)], limit=1
        )
        if not account:
            return request.make_response("no active account", status=404)
        # Valida firma para evitar requests falsos
        if not account.is_valid_signature(raw_body, signature):
            _logger.warning("Invalid Meta webhook signature.")
            return request.make_response("invalid signature", status=403)

        # Decodifica body a texto y parsea JSON
        raw_text = raw_body.decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw_text) if raw_text else {}
        except Exception as err:
            # Si falla el parseo, guardamos evidencia para diagnóstico
            _logger.warning("Webhook JSON invalido. error=%s raw=%s", err, raw_text)
            payload = {"_parse_error": str(err), "_raw_body": raw_text}

        # Guardamos evento inbound en log para auditoría
        request.env["whatsapp.meta.log"].sudo().create(
            {
                "account_id": account.id,
                "direction": "in",
                "operation": "webhook_event",
                "request_payload": json.dumps(payload),
                "response_status": 200,
                "response_body": "ok",
            }
        )
        # Procesa payload: publica mensajes en chatter de órdenes de venta
        posted = account.sudo().process_webhook_payload(payload)
        _logger.info("WhatsApp webhook procesado. Publicaciones en chatter/chat=%s", posted)
        return request.make_response("ok", status=200)
