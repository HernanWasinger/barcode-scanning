# -*- coding: utf-8 -*-
"""
Módulo principal de integración con WhatsApp Cloud API (Meta).

Contiene:
- WhatsappMetaAccount: modelo de cuenta con credenciales y lógica de envío/recepción
- WhatsappMetaLog: bitácora de requests/responses hacia Meta
- WhatsappMetaMessage: registro de IDs de mensajes para deduplicar eventos
- WhatsappMetaTemplate: plantillas sincronizadas desde Meta
"""

# hashlib: para calcular HMAC SHA256 al validar firma del webhook
import hashlib
# hmac: para comparar firmas de forma segura (evita timing attacks)
import hmac
# json: para parsear payloads del webhook y respuestas de la API
import json
# logging: para registrar eventos y errores
import logging
# re: expresiones regulares para validar formato de teléfono y API version
import re
# datetime: para formatear fechas en templates de orden
from datetime import datetime

# requests: librería HTTP para llamar a Graph API de Meta
import requests

# _: función de traducción de cadenas (i18n)
# api: decoradores @api.depends, @api.onchange, @api.constrains, @api.model
# fields: definición de campos del modelo
# models: base para modelos Odoo
from odoo import _, api, fields, models
# UserError: excepción mostrada al usuario en UI
# ValidationError: excepción al fallar validaciones de campos
from odoo.exceptions import UserError, ValidationError

# Logger con nombre del módulo para filtrar en logs
_logger = logging.getLogger(__name__)
# Templates reservados por Meta para números de prueba (sandbox)
TEST_TEMPLATE_NAMES = {"hello_world"}


class WhatsappMetaAccount(models.Model):
    """Cuenta de integracion con WhatsApp Cloud API (Meta).

    Este modelo concentra toda la configuracion tecnica:
    - credenciales (token, app secret)
    - identificadores de API (phone_number_id / WABA)
    - webhook (verify token + URL)
    - pruebas de conexion/envio
    - bitacora de requests/responses
    """

    # Identificador técnico del modelo en Odoo
    _name = "whatsapp.meta.account"
    # Hereda mail.thread para tener chatter (mensajes) en el formulario de la cuenta
    _inherit = ["mail.thread"]
    # Descripción legible para desarrolladores
    _description = "WhatsApp Meta Account"

    # --- Campos de configuración ---
    # Nombre visible en la UI (ej: "Meta WhatsApp")
    name = fields.Char(required=True, default="Meta WhatsApp")
    # Si está activa, se usa para envíos; solo puede haber una activa
    active = fields.Boolean(default=True)
    # Token de acceso permanente de la app de Meta (Bearer token)
    token = fields.Char(required=True)
    # App Secret: se usa para validar firma HMAC del webhook (seguridad)
    app_secret = fields.Char(
        required=True, help="App Secret para validar firma del webhook."
    )
    # Token que Meta envía en GET; debe coincidir con el configurado en Meta
    verify_token = fields.Char(
        required=True, help="Token de verificacion para el webhook de Meta."
    )
    # ID del número de teléfono en WhatsApp Cloud API (solo dígitos)
    phone_number_id = fields.Char(
        required=True, help="Phone Number ID de WhatsApp Cloud API."
    )
    # ID de la cuenta de negocio (WABA); opcional, necesario para sincronizar templates
    business_account_id = fields.Char(help="WhatsApp Business Account ID (opcional).")
    # Versión de Graph API (ej: "23.0")
    api_version = fields.Char(default="23.0", required=True)
    # Nombre del template a usar por defecto (ej: "hello_world")
    template_name = fields.Char(default="hello_world", required=True)
    # Código de idioma del template (ej: es_AR, en_US)
    template_language = fields.Selection(
        [
            ("es_AR", "Español (Argentina)"),
            ("es_419", "Español (Latinoamérica)"),
            ("es_ES", "Español (España)"),
            ("es_MX", "Español (México)"),
            ("es_CO", "Español (Colombia)"),
            ("es_CL", "Español (Chile)"),
            ("es_PE", "Español (Perú)"),
            ("es_UY", "Español (Uruguay)"),
            ("es_VE", "Español (Venezuela)"),
            ("es", "Español"),
            ("en_US", "English (US)"),
        ],
        default="es_AR",
        required=True,
    )
    # Referencia a template sincronizado desde Meta (opcional, facilita selección)
    template_id = fields.Many2one(
        "whatsapp.meta.template",
        string="Template aprobado",
        domain="[('account_id', '=', id), ('status', '=', 'APPROVED'), ('language_code', 'ilike', 'es%')]",
        help="Template sincronizado desde Meta para usar en envios.",
    )
    # URL pública del webhook (calculada desde web.base.url)
    webhook_url = fields.Char(compute="_compute_webhook_url")
    # True si la última prueba de conexión fue exitosa
    connected = fields.Boolean(readonly=True)
    # Código HTTP de la última respuesta (200, 400, etc.)
    last_status_code = fields.Integer(readonly=True)
    # Respuesta cruda de la última llamada
    last_response = fields.Text(readonly=True)
    # Versión legible para mostrar en UI (resumen formateado)
    last_response_readable = fields.Text(
        compute="_compute_last_response_readable",
        readonly=True,
    )
    # Historial de logs (requests/responses)
    log_ids = fields.One2many("whatsapp.meta.log", "account_id")
    # Templates sincronizados desde Meta
    template_ids = fields.One2many("whatsapp.meta.template", "account_id")

    @api.onchange("template_id")
    def _onchange_template_id(self):
        """Al seleccionar template sincronizado, completa nombre e idioma."""
        # Obtiene códigos de idioma soportados en la selección del campo
        supported_languages = {
            code for code, _label in self._fields["template_language"].selection
        }
        for rec in self:
            if rec.template_id:
                # Sincroniza nombre del template
                rec.template_name = rec.template_id.name
                # Sincroniza idioma si está en la lista soportada
                if rec.template_id.language_code in supported_languages:
                    rec.template_language = rec.template_id.language_code

    @api.depends("last_response", "last_status_code")
    def _compute_last_response_readable(self):
        """Muestra siempre una respuesta legible en UI aunque haya JSON crudo."""
        for rec in self:
            text = rec.last_response or ""
            parsed = None
            try:
                # Intenta parsear como JSON para extraer resumen
                parsed = json.loads(text) if text else None
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                # Si es dict, construye resumen legible según tipo de operación
                rec.last_response_readable = rec._build_ui_response_summary(
                    operation="ultima_respuesta",
                    status=rec.last_status_code or 0,
                    body=text,
                )
            else:
                # Si no es JSON, muestra el texto tal cual
                rec.last_response_readable = text

    @api.depends("verify_token")
    def _compute_webhook_url(self):
        """Construye la URL publica del webhook usando web.base.url.

        Nota: Meta necesita una URL HTTPS publica para validar y enviar eventos.
        """
        # Obtiene URL base del sistema (ej: https://miempresa.odoo.com)
        base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
        for rec in self:
            # Ruta fija que el controlador webhook escucha
            rec.webhook_url = f"{base_url}/whatsapp/meta/webhook"

    @api.constrains("phone_number_id")
    def _check_phone_number_id(self):
        """Valida que el Phone Number ID venga como valor numerico."""
        for rec in self:
            if not rec.phone_number_id.isdigit():
                raise ValidationError(_("Phone Number ID debe contener solo digitos."))

    @api.constrains("api_version")
    def _check_api_version(self):
        """Valida formato de version de Graph API (ej: 23.0)."""
        for rec in self:
            # Debe ser formato "número.número" (ej: 23.0)
            if not re.match(r"^\d+\.\d+$", rec.api_version or ""):
                raise ValidationError(_("La version API debe tener formato '23.0'."))

    @api.constrains("active")
    def _check_single_active(self):
        """Fuerza una unica cuenta activa para evitar envios ambiguos."""
        for rec in self.filtered("active"):
            count = self.search_count([("active", "=", True)])
            if count > 1:
                raise ValidationError(_("Solo puede haber una cuenta activa de Meta."))

    def _build_graph_url(self, endpoint):
        """Arma URL absoluta de Graph API para un endpoint dado."""
        self.ensure_one()
        # Ej: https://graph.facebook.com/v23.0/123456789/messages
        return f"https://graph.facebook.com/v{self.api_version}/{endpoint}"

    def _build_messages_url(self):
        """Endpoint de envio de mensajes para el numero configurado."""
        self.ensure_one()
        return self._build_graph_url(f"{self.phone_number_id}/messages")

    def _allow_test_templates(self):
        """Detecta automaticamente si el numero emisor es de prueba (sandbox)."""
        self.ensure_one()
        if not self.business_account_id:
            return False
        try:
            # Consulta API para obtener account_mode del número
            url = self._build_graph_url(f"{self.business_account_id}/phone_numbers")
            response = requests.get(
                url,
                headers={"Authorization": f"Bearer {self.token}"},
                params={"fields": "id,account_mode", "limit": 100},
                timeout=20,
            )
            response.raise_for_status()
            data = response.json() or {}
            for item in data.get("data", []):
                # Busca el número que coincide con phone_number_id
                if str(item.get("id")) != str(self.phone_number_id):
                    continue
                # Si es SANDBOX, permite templates de prueba (hello_world)
                return (item.get("account_mode") or "").upper() == "SANDBOX"
        except Exception:
            _logger.warning("No se pudo detectar account_mode para numero de prueba.")
        return False

    def _normalize_phone(self, phone):
        """Normaliza telefono a solo digitos para campo `to` de Meta.

        WhatsApp Cloud API espera formato internacional sin simbolos.
        """
        if not phone:
            raise UserError(_("Debe indicar un numero de destino."))
        # Elimina todo lo que no sea dígito
        phone_digits = re.sub(r"\D", "", phone or "")
        if len(phone_digits) < 8:
            raise UserError(_("Numero invalido: %s") % phone)
        return phone_digits

    def _normalize_phone_soft(self, phone):
        """Normaliza sin validar duro para payloads entrantes del webhook."""
        return re.sub(r"\D", "", phone or "")

    def _find_partner_from_inbound_number(self, from_number):
        """Busca partner por telefono/celular usando match por sufijo."""
        digits = self._normalize_phone_soft(from_number)
        if not digits:
            return self.env["res.partner"]
        # Usa últimos 10 dígitos para tolerar 54 vs 549, etc.
        tail = digits[-10:]
        pattern = "%" + tail  # ilike necesita % para match por sufijo
        return self.env["res.partner"].search(
            [
                "|",
                "|",
                ("phone", "ilike", pattern),
                ("mobile", "ilike", pattern),
                ("phone_sanitized", "ilike", pattern),
            ],
            limit=1,
            order="id desc",
        )

    def _build_inbound_chatter_body(self, from_number, text, message_type, message_id):
        """Devuelve solo el contenido visible del mensaje del cliente."""
        incoming_text = (text or "").strip()
        if incoming_text:
            return incoming_text
        return _("(mensaje entrante sin texto)")

    def _find_target_sale_order(self, from_number, partner=False):
        """Busca la orden vinculada al ultimo envio saliente a ese numero."""
        normalized = self._normalize_phone_soft(from_number)
        if not normalized:
            return self.env["sale.order"]
        # Match por sufijo para tolerar formatos 54... vs 549...
        tail = normalized[-10:]
        pattern = "%" + tail  # ilike necesita % para match por sufijo
        # Busca último mensaje saliente a ese número con orden asociada
        outbound = self.env["whatsapp.meta.message"].sudo().search(
            [
                ("account_id", "=", self.id),
                ("direction", "=", "out"),
                ("sale_order_id", "!=", False),
                ("phone_number", "ilike", pattern),
            ],
            order="id desc",
            limit=1,
        )
        if outbound and outbound.sale_order_id:
            return outbound.sale_order_id
        # Fallback: última orden del partner si lo conocemos
        if partner:
            return self.env["sale.order"].search(
                [("partner_id", "=", partner.id)],
                order="id desc",
                limit=1,
            )
        return self.env["sale.order"]

    def process_webhook_payload(self, payload):
        """Procesa eventos webhook y publica inbound en chatter de la orden de venta.

        Solo publica en sale.order, NO en res.partner (evita historial en contactos).
        """
        self.ensure_one()
        posted_count = 0
        # Recorre estructura de payload de Meta: entry -> changes -> messages
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                if change.get("field") != "messages":
                    continue
                value = change.get("value", {})
                for message in value.get("messages", []):
                    from_number = self._normalize_phone_soft(message.get("from"))
                    message_id = message.get("id")
                    # Evita duplicados por retries de Meta.
                    if message_id and self.env["whatsapp.meta.message"].search_count(
                        [("account_id", "=", self.id), ("meta_message_id", "=", message_id)]
                    ):
                        continue
                    message_type = message.get("type")
                    text = message.get("text", {}).get("body")
                    body = self._build_inbound_chatter_body(
                        from_number=from_number,
                        text=text,
                        message_type=message_type,
                        message_id=message_id,
                    )
                    partner = self._find_partner_from_inbound_number(from_number)
                    sale_order = self._find_target_sale_order(from_number, partner=partner)
                    if message_id:
                        # Registra mensaje para deduplicación futura
                        self.env["whatsapp.meta.message"].sudo().create(
                            {
                                "account_id": self.id,
                                "meta_message_id": message_id,
                                "direction": "in",
                                "phone_number": from_number,
                                "sale_order_id": sale_order.id if sale_order else False,
                            }
                        )
                    if sale_order:
                        # Solo publica si la conversación está abierta
                        if hasattr(sale_order, "whatsapp_conversation_state") and (
                            sale_order.whatsapp_conversation_state != "open"
                        ):
                            _logger.info(
                                "WhatsApp inbound: orden %s encontrada pero conversacion cerrada. from=%s",
                                sale_order.name,
                                from_number,
                            )
                            continue
                        # Publica en chatter de la orden (autor: partner o cliente del pedido)
                        author_id = partner.id if partner else sale_order.partner_id.id
                        sale_order.with_context(
                            mail_notify_noemail=True,
                            no_email=True,
                            whatsapp_skip_outbound_sync=True,
                        ).message_post(
                            body=body,
                            message_type="comment",
                            subtype_xmlid="mail.mt_comment",
                            author_id=author_id,
                        )
                        posted_count += 1
                    elif partner:
                        _logger.warning(
                            "WhatsApp inbound sin orden vinculada. from=%s id=%s",
                            from_number,
                            message_id,
                        )
                        self.with_context(
                            mail_notify_noemail=True,
                            no_email=True,
                            whatsapp_skip_outbound_sync=True,
                        ).message_post(
                            body=body,
                            message_type="comment",
                            subtype_xmlid="mail.mt_note",
                            author_id=partner.id,
                        )
                        posted_count += 1
                    else:
                        _logger.info(
                            "WhatsApp inbound: no orden ni partner para from=%s id=%s",
                            from_number,
                            message_id,
                        )
                        self.with_context(
                            mail_notify_noemail=True,
                            no_email=True,
                            whatsapp_skip_outbound_sync=True,
                        ).message_post(
                            body=body,
                            message_type="comment",
                            subtype_xmlid="mail.mt_note",
                        )
                        posted_count += 1
        return posted_count

    def _log_exchange(self, operation, payload, response):
        """Persiste request/response para auditoria y soporte tecnico."""
        self.ensure_one()
        body = response.text
        status = response.status_code
        # Guardamos ultimo resultado visible en la cuenta en formato legible.
        ui_summary = self._build_ui_response_summary(operation, status, body)
        self.sudo().write({"last_status_code": status, "last_response": ui_summary})
        # Guardamos historico completo de llamadas.
        self.env["whatsapp.meta.log"].sudo().create(
            {
                "account_id": self.id,
                "operation": operation,
                "direction": "out",
                "request_payload": json.dumps(payload),
                "response_status": status,
                "response_body": body,
            }
        )

    def _raise_meta_http_error(self, response, default_message):
        """Convierte errores HTTP de Meta en UserError con detalle util."""
        self.ensure_one()
        status = response.status_code
        message = default_message
        code = "-"
        subcode = "-"
        trace = "-"
        try:
            parsed = response.json() or {}
        except Exception:
            parsed = {}
        error = parsed.get("error") if isinstance(parsed, dict) else {}
        if isinstance(error, dict):
            message = error.get("message") or message
            code = error.get("code") or "-"
            subcode = error.get("error_subcode") or "-"
            trace = error.get("fbtrace_id") or "-"
        details = _(
            "Meta rechazo la operacion.\nHTTP: %(http)s\nCode: %(code)s\nSubcode: %(sub)s\nTrace: %(trace)s\nDetalle: %(msg)s"
        ) % {
            "http": status,
            "code": code,
            "sub": subcode,
            "trace": trace,
            "msg": message,
        }
        raise UserError(details)

    def _build_ui_response_summary(self, operation, status, body):
        """Convierte respuesta de Meta a un resumen legible para UI."""
        self.ensure_one()
        lines = [_("Operacion: %s") % operation, _("HTTP: %s") % status]
        data = {}
        try:
            data = json.loads(body or "{}")
        except Exception:
            data = {}

        if isinstance(data, dict) and data.get("error"):
            error = data.get("error") or {}
            lines.append(_("Estado: error"))
            lines.append(_("Codigo: %s") % (error.get("code") or "-"))
            lines.append(_("Subcodigo: %s") % (error.get("error_subcode") or "-"))
            lines.append(_("Trace: %s") % (error.get("fbtrace_id") or "-"))
            lines.append(_("Detalle: %s") % (error.get("message") or _("Sin detalle")))
            return "\n".join(lines)

        if operation == "test_connection":
            lines.append(_("Estado: conexion validada"))
            lines.append(_("Numero verificado: %s") % (data.get("display_phone_number") or "-"))
            lines.append(_("Nombre verificado: %s") % (data.get("verified_name") or "-"))
            lines.append(
                _("Code verification status: %s")
                % (data.get("code_verification_status") or "-")
            )
            lines.append(_("Name status: %s") % (data.get("name_status") or "-"))
            return "\n".join(lines)

        if operation in ("send_order_template", "send_order_text", "send_test_template"):
            contacts = data.get("contacts") if isinstance(data, dict) else []
            messages = data.get("messages") if isinstance(data, dict) else []
            wa_id = contacts and contacts[0].get("wa_id") or "-"
            meta_id = messages and messages[0].get("id") or "-"
            lines.append(_("Estado: enviado"))
            lines.append(_("Destino WhatsApp: %s") % wa_id)
            lines.append(_("ID mensaje Meta: %s") % meta_id)
            return "\n".join(lines)

        if operation == "sync_templates":
            templates = data.get("data") if isinstance(data, dict) else []
            approved = [
                tpl for tpl in templates if (tpl.get("status") or "").upper() == "APPROVED"
            ]
            approved_es = [
                tpl
                for tpl in approved
                if (tpl.get("language") or "").lower().startswith("es")
            ]
            lines.append(_("Estado: sincronizacion completada"))
            lines.append(_("Templates recibidos (pagina): %s") % len(templates))
            lines.append(_("Aprobados (pagina): %s") % len(approved))
            lines.append(_("Aprobados en espanol (pagina): %s") % len(approved_es))
            return "\n".join(lines)

        lines.append(_("Estado: operacion completada"))
        return "\n".join(lines)

    def _send_payload(self, payload, operation="send_template", allow_param_mismatch_retry=False):
        """Envia payload a Meta y convierte errores HTTP en UserError legible."""
        self.ensure_one()
        url = self._build_messages_url()
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        self._log_exchange(operation=operation, payload=payload, response=response)
        _logger.info("Meta WA send url=%s payload=%s", url, json.dumps(payload))
        _logger.info(
            "Meta WA response status=%s body=%s", response.status_code, response.text
        )
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError:
            # Fallback defensivo: algunos templates aprobados no esperan parametros.
            # Si Meta responde 132000 y habia components, reintentamos sin components.
            if allow_param_mismatch_retry:
                err = {}
                try:
                    err = (response.json() or {}).get("error") or {}
                except Exception:
                    err = {}
                error_code = str(err.get("code") or "")
                if (
                    error_code == "132000"
                    and isinstance(payload.get("template"), dict)
                    and payload["template"].get("components")
                ):
                    retry_payload = dict(payload)
                    retry_template = dict(retry_payload.get("template") or {})
                    retry_template.pop("components", None)
                    retry_payload["template"] = retry_template
                    retry_response = requests.post(
                        url, headers=headers, json=retry_payload, timeout=20
                    )
                    self._log_exchange(
                        operation=f"{operation}_retry_without_components",
                        payload=retry_payload,
                        response=retry_response,
                    )
                    _logger.info(
                        "Meta WA retry without components status=%s body=%s",
                        retry_response.status_code,
                        retry_response.text,
                    )
                    try:
                        retry_response.raise_for_status()
                    except requests.exceptions.HTTPError:
                        self._raise_meta_http_error(
                            retry_response,
                            default_message=_(
                                "Error en envio de mensaje a WhatsApp Cloud API."
                            ),
                        )
                    return retry_response.json()
            self._raise_meta_http_error(
                response,
                default_message=_("Error en envio de mensaje a WhatsApp Cloud API."),
            )
        return response.json()

    def _build_template_payload(self, to_number, template_name=None, language=None):
        """Construye payload de mensaje tipo `template`.

        Se utiliza para envio de prueba y para pedidos.
        """
        self.ensure_one()
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": self._normalize_phone(to_number),
            "type": "template",
            "template": {
                "name": template_name or self.template_name,
                "language": {"code": language or self.template_language},
            },
        }

    def _truncate_text(self, value, max_len=200):
        """Recorta texto para no exceder limites de plantilla."""
        text = (value or "").strip()
        if len(text) <= max_len:
            return text
        return f"{text[: max_len - 3]}..."

    def _format_amount(self, amount, currency):
        """Formatea importes en formato legible."""
        symbol = currency.symbol if currency else ""
        value = f"{amount:.2f}" if isinstance(amount, (int, float)) else str(amount or "0.00")
        return f"{symbol} {value}".strip()

    def _build_order_lines_summary(self, order, max_lines=5):
        """Resume lineas de pedido para enviar por template."""
        chunks = []
        for line in order.order_line[:max_lines]:
            line_name = self._truncate_text(line.name or line.product_id.display_name or "-", 45)
            chunks.append(f"- {line_name} x{line.product_uom_qty}")
        remaining = len(order.order_line) - len(chunks)
        if remaining > 0:
            chunks.append(_("(+ %s lineas mas)") % remaining)
        return "\n".join(chunks) or "-"

    def _get_order_summary_plain_text(self, order):
        """Genera el texto plano del resumen del pedido para enviar por WhatsApp."""
        self.ensure_one()
        currency = order.currency_id
        order_date = order.date_order
        if isinstance(order_date, datetime):
            date_str = order_date.strftime("%d/%m/%Y %H:%M")
        else:
            date_str = str(order_date or "-")
        lines = [
            _("Pedido: %s") % (order.name or "-"),
            _("Fecha: %s") % date_str,
            _("Cliente: %s") % (order.partner_id.display_name or "-"),
            _("Vendedor: %s") % (order.user_id.display_name or "-"),
            "",
            _("Importes:"),
            _("  Subtotal: %s") % self._format_amount(order.amount_untaxed, currency),
            _("  Impuestos: %s") % self._format_amount(order.amount_tax, currency),
            _("  Total: %s (%s)") % (
                self._format_amount(order.amount_total, currency),
                currency.name or "-",
            ),
            "",
            _("Líneas:"),
            self._build_order_lines_summary(order, max_lines=10),
        ]
        text = "\n".join(lines)
        parts = re.split(r"\s*Notas:\s*", text, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) > 1:
            text = parts[0]
        text = re.sub(r"\n\[\d+\]\s*https?://\S*", "", text)
        text = re.sub(r"\n\[\d+\]\s*$", "", text, flags=re.MULTILINE)
        text = re.sub(r"\n\s*https?://\S*", "", text)
        return text.strip()

    def _build_order_template_components(self, order):
        """Construye variables del template de orden de venta para Meta.

        Orden de variables esperadas en el body del template:
        1=cliente, 2=resumen cabecera, 3=resumen importes,
        4=detalle lineas, 5=notas.
        """
        currency = order.currency_id
        order_date = order.date_order
        if isinstance(order_date, datetime):
            date_str = order_date.strftime("%d/%m/%Y %H:%M")
        else:
            date_str = str(order_date or "-")
        conversation_event = (self.env.context.get("whatsapp_conversation_event") or "open").lower()
        closing_text = _("Cerrando conversacion por orden de venta %s") % (order.name or "-")
        note = self._truncate_text(order.note or "-", 300)
        lines_summary = self._truncate_text(self._build_order_lines_summary(order), 900)
        if conversation_event == "close":
            header_summary = self._truncate_text(
                _("%s | Orden %s | Fecha %s | Vendedor %s | Empresa %s")
                % (
                    closing_text,
                    order.name or "-",
                    date_str,
                    order.user_id.display_name or "-",
                    order.company_id.display_name or "-",
                ),
                280,
            )
        else:
            header_summary = self._truncate_text(
                _("Orden %s | Fecha %s | Vendedor %s | Empresa %s")
                % (
                    order.name or "-",
                    date_str,
                    order.user_id.display_name or "-",
                    order.company_id.display_name or "-",
                ),
                280,
            )
        amounts_summary = self._truncate_text(
            _(
                "Subtotal: %s | Impuestos: %s | Total: %s (%s)"
            )
            % (
                self._format_amount(order.amount_untaxed, currency),
                self._format_amount(order.amount_tax, currency),
                self._format_amount(order.amount_total, currency),
                currency.name or "-",
            ),
            250,
        )
        params = [
            order.partner_id.display_name or "-",
            header_summary,
            amounts_summary,
            lines_summary,
            note,
        ]
        expected_count = 5
        if self.env.context.get("whatsapp_expected_params_count") is not None:
            expected_count = int(self.env.context.get("whatsapp_expected_params_count") or 0)
        if expected_count <= 0:
            return []
        # Si el template acepta solo un parametro, para cierre usamos mensaje de cierre.
        if expected_count == 1:
            params = [closing_text] if conversation_event == "close" else [params[0]]
        params = params[:expected_count]
        return [
            {
                "type": "body",
                "parameters": [{"type": "text", "text": str(value)} for value in params],
            }
        ]

    def _extract_template_body_params_count(self, template_payload):
        """Obtiene cantidad de placeholders {{n}} del BODY del template."""
        components = []
        if isinstance(template_payload, dict):
            components = template_payload.get("components") or []
        if not isinstance(components, list):
            return 0
        body_text = ""
        for component in components:
            if not isinstance(component, dict):
                continue
            if (component.get("type") or "").upper() == "BODY":
                body_text = component.get("text") or ""
                break
        if not body_text:
            return 0
        placeholders = re.findall(r"\{\{(\d+)\}\}", body_text)
        # Cuenta placeholders unicos para evitar duplicados accidentales.
        return len(set(placeholders))

    def action_sync_templates(self):
        """Sincroniza templates de Meta y guarda solo los aceptados/visibles."""
        for rec in self:
            if not rec.business_account_id:
                raise UserError(
                    _("Complete el campo 'Business Account ID' para sincronizar templates.")
                )
            url = rec._build_graph_url(f"{rec.business_account_id}/message_templates")
            params = {
                "fields": "id,name,language,status,category,quality_score,components",
                "limit": 100,
            }
            imported = 0
            while url:
                response = requests.get(
                    url,
                    headers={"Authorization": f"Bearer {rec.token}"},
                    params=params,
                    timeout=20,
                )
                rec._log_exchange(
                    operation="sync_templates",
                    payload={"url": url, "params": params},
                    response=response,
                )
                try:
                    response.raise_for_status()
                except requests.exceptions.HTTPError:
                    rec._raise_meta_http_error(
                        response,
                        default_message=_("No se pudieron sincronizar templates desde Meta."),
                    )
                data = response.json() or {}
                for item in data.get("data", []):
                    values = {
                        "account_id": rec.id,
                        "meta_template_id": item.get("id"),
                        "name": item.get("name"),
                        "language_code": item.get("language") or "es_AR",
                        "status": item.get("status") or "UNKNOWN",
                        "category": item.get("category"),
                        "quality_score": (
                            (item.get("quality_score") or {}).get("score")
                            if isinstance(item.get("quality_score"), dict)
                            else item.get("quality_score")
                        )
                        or "",
                        "body_params_count": rec._extract_template_body_params_count(item),
                        "is_test_template": (item.get("name") or "").lower()
                        in TEST_TEMPLATE_NAMES,
                    }
                    if not values["meta_template_id"] or not values["name"]:
                        continue
                    existing = self.env["whatsapp.meta.template"].sudo().search(
                        [
                            ("account_id", "=", rec.id),
                            ("meta_template_id", "=", values["meta_template_id"]),
                        ],
                        limit=1,
                    )
                    if existing:
                        existing.write(values)
                    else:
                        self.env["whatsapp.meta.template"].sudo().create(values)
                    imported += 1
                paging = data.get("paging", {}) if isinstance(data, dict) else {}
                url = paging.get("next")
                params = None

            approved_spanish = self.env["whatsapp.meta.template"].search(
                [
                    ("account_id", "=", rec.id),
                    ("status", "=", "APPROVED"),
                    ("language_code", "ilike", "es%"),
                ],
                order="id desc",
                limit=1,
            )
            if approved_spanish:
                rec.template_id = approved_spanish.id
                rec.template_name = approved_spanish.name
                supported_languages = {
                    code for code, _label in rec._fields["template_language"].selection
                }
                if approved_spanish.language_code in supported_languages:
                    rec.template_language = approved_spanish.language_code
        return True

    def _build_text_payload(self, to_number, text):
        """Construye payload de texto libre para conversaciones activas."""
        clean_text = (text or "").strip()
        if not clean_text:
            raise UserError(_("No se puede enviar un mensaje vacio."))
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": self._normalize_phone(to_number),
            "type": "text",
            "text": {"preview_url": False, "body": clean_text},
        }

    def action_test_connection(self):
        """Valida conectividad y permisos consultando el phone_number_id."""
        for rec in self:
            url = rec._build_graph_url(rec.phone_number_id)
            response = requests.get(
                url,
                headers={"Authorization": f"Bearer {rec.token}"},
                params={
                    "fields": "id,display_phone_number,verified_name,code_verification_status,name_status"
                },
                timeout=20,
            )
            rec._log_exchange(
                operation="test_connection",
                payload={
                    "url": url,
                    "fields": "id,display_phone_number,verified_name,code_verification_status,name_status",
                },
                response=response,
            )
            try:
                response.raise_for_status()
            except requests.exceptions.HTTPError:
                # Si falla, marcamos estado de conexion como no valido.
                rec.connected = False
                rec._raise_meta_http_error(
                    response,
                    default_message=_("No se pudo validar la conexion con Meta."),
                )
            # Si responde 200, la cuenta queda marcada como conectada.
            rec.connected = True
        return True

    def action_send_test_template(self):
        """Abre wizard para ingresar numero y template al enviar prueba."""
        self.ensure_one()
        allow_test_templates = self._allow_test_templates()
        approved_templates = self.env["whatsapp.meta.template"].search(
            [("account_id", "=", self.id), ("status", "=", "APPROVED")]
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
                    "No hay templates aprobados para prueba. Sincroniza templates y reintenta."
                )
            )
        return {
            "type": "ir.actions.act_window",
            "name": _("Enviar prueba WhatsApp"),
            "res_model": "whatsapp.test.send.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_account_id": self.id,
                "default_template_id": approved_templates.id,
                "default_allow_test_templates": allow_test_templates,
            },
        }

    def send_order_template(
        self,
        order,
        template=False,
        force_no_components=False,
        conversation_event="open",
    ):
        """Envia un template al cliente de una orden de venta."""
        self.ensure_one()
        partner_phone = order.partner_id.mobile or order.partner_id.phone
        if not partner_phone:
            raise UserError(_("El cliente no tiene telefono o celular cargado."))
        # Regla estricta: las ordenes siempre salen al numero del cliente del pedido.
        destination_phone = self._normalize_phone(partner_phone)
        selected_template_name = self.template_name
        selected_template_language = self.template_language
        selected_template = template
        if not selected_template:
            selected_template = self.env["whatsapp.meta.template"].search(
                [("account_id", "=", self.id), ("status", "=", "APPROVED")],
                order="id desc",
                limit=1,
            )
        if selected_template and selected_template.status == "APPROVED":
            selected_template_name = selected_template.name
            selected_template_language = selected_template.language_code
        payload = self._build_template_payload(
            destination_phone,
            template_name=selected_template_name,
            language=selected_template_language,
        )
        if not force_no_components:
            expected_params_count = (
                int(selected_template.body_params_count) if selected_template else 5
            )
            if expected_params_count > 5:
                raise UserError(
                    _(
                        "El template '%s' requiere %s parametros, pero este flujo solo puede enviar hasta 5."
                    )
                    % (selected_template_name, expected_params_count)
                )
            components = self.with_context(
                whatsapp_expected_params_count=expected_params_count,
                whatsapp_conversation_event=conversation_event,
            )._build_order_template_components(order)
            if components:
                payload["template"]["components"] = components
        result = self._send_payload(
            payload,
            operation="send_order_template",
            allow_param_mismatch_retry=True,
        )
        meta_message_id = (
            result.get("messages") and result["messages"][0].get("id") or False
        )
        if meta_message_id:
            wa_id = (
                result.get("contacts")
                and result["contacts"][0].get("wa_id")
                or destination_phone
            )
            self.env["whatsapp.meta.message"].sudo().create(
                {
                    "account_id": self.id,
                    "meta_message_id": meta_message_id,
                    "direction": "out",
                    "phone_number": self._normalize_phone_soft(wa_id),
                    "sale_order_id": order.id,
                }
            )
        contact_wa = (
            result.get("contacts")
            and result["contacts"][0].get("wa_id")
            or destination_phone
        )
        order.with_context(whatsapp_skip_outbound_sync=True).message_post(
            body=_(
                "Template WhatsApp enviado.\n"
                "Cliente: %s\n"
                "Template: %s (%s)\n"
                "Destino: %s\n"
                "Estado: accepted"
            )
            % (
                order.partner_id.display_name,
                selected_template_name,
                selected_template_language,
                contact_wa,
            ),
            message_type="comment",
            subtype_xmlid="mail.mt_note",
        )
        return result

    def send_order_text(self, order, text):
        """Envia texto libre al numero del cliente de la orden."""
        self.ensure_one()
        partner_phone = order.partner_id.mobile or order.partner_id.phone
        if not partner_phone:
            raise UserError(_("El cliente no tiene telefono o celular cargado."))
        destination_phone = self._normalize_phone(partner_phone)
        payload = self._build_text_payload(destination_phone, text)
        result = self._send_payload(payload, operation="send_order_text")
        meta_message_id = (
            result.get("messages") and result["messages"][0].get("id") or False
        )
        if meta_message_id:
            wa_id = (
                result.get("contacts")
                and result["contacts"][0].get("wa_id")
                or destination_phone
            )
            self.env["whatsapp.meta.message"].sudo().create(
                {
                    "account_id": self.id,
                    "meta_message_id": meta_message_id,
                    "direction": "out",
                    "phone_number": self._normalize_phone_soft(wa_id),
                    "sale_order_id": order.id,
                }
            )
        order.with_context(whatsapp_skip_outbound_sync=True).message_post(
            body=_("Mensaje WhatsApp enviado al cliente."),
            message_type="comment",
            subtype_xmlid="mail.mt_note",
        )
        return result

    def has_inbound_for_order(self, order):
        """Indica si la orden ya recibio al menos un mensaje entrante."""
        self.ensure_one()
        return bool(
            self.env["whatsapp.meta.message"].sudo().search_count(
                [
                    ("account_id", "=", self.id),
                    ("sale_order_id", "=", order.id),
                    ("direction", "=", "in"),
                ]
            )
        )

    def is_valid_signature(self, raw_body, signature):
        """Valida firma HMAC SHA256 enviada por Meta en webhook POST."""
        self.ensure_one()
        if not signature:
            return False
        expected = (
            "sha256="
            + hmac.new(
                self.app_secret.encode("utf-8"), raw_body, hashlib.sha256
            ).hexdigest()
        )
        return hmac.compare_digest(expected, signature)

    @api.model
    def get_default_account(self):
        """Devuelve la cuenta activa por defecto para operaciones de envio."""
        account = self.search([("active", "=", True)], limit=1)
        if not account:
            raise UserError(_("No hay una cuenta activa de WhatsApp Meta configurada."))
        return account

    @api.model
    def get_account_by_verify_token(self, token):
        """Busca cuenta activa para validar handshake GET de webhook."""
        return self.search([("verify_token", "=", token), ("active", "=", True)], limit=1)


class WhatsappMetaLog(models.Model):
    """Bitacora tecnica de trafico con Meta (entrada/salida)."""

    _name = "whatsapp.meta.log"
    _description = "WhatsApp Meta Log"
    _order = "create_date desc"

    account_id = fields.Many2one("whatsapp.meta.account", required=True, ondelete="cascade")
    direction = fields.Selection(
        [("in", "Inbound"), ("out", "Outbound")], required=True, default="out"
    )
    operation = fields.Char(required=True)
    request_payload = fields.Text()
    response_status = fields.Integer()
    response_body = fields.Text()


class WhatsappMetaMessage(models.Model):
    """Registro tecnico de IDs de Meta para deduplicar eventos."""

    _name = "whatsapp.meta.message"
    _description = "WhatsApp Meta Message"
    _order = "id desc"

    account_id = fields.Many2one("whatsapp.meta.account", required=True, ondelete="cascade")
    meta_message_id = fields.Char(required=True, index=True)
    direction = fields.Selection([("in", "Inbound"), ("out", "Outbound")], required=True)
    phone_number = fields.Char(index=True)
    sale_order_id = fields.Many2one("sale.order", ondelete="set null", index=True)

    _sql_constraints = [
        (
            "whatsapp_meta_message_unique",
            "unique(account_id, meta_message_id)",
            "El mensaje de Meta ya fue procesado.",
        )
    ]


class WhatsappMetaTemplate(models.Model):
    """Templates sincronizados desde WhatsApp Business (Meta)."""

    _name = "whatsapp.meta.template"
    _description = "WhatsApp Meta Template"
    _order = "name asc, language_code asc"

    account_id = fields.Many2one("whatsapp.meta.account", required=True, ondelete="cascade")
    meta_template_id = fields.Char(required=True, index=True)
    name = fields.Char(required=True, index=True)
    language_code = fields.Char(required=True, default="es_AR")
    status = fields.Selection(
        [
            ("APPROVED", "Aprobado"),
            ("PENDING", "Pendiente"),
            ("REJECTED", "Rechazado"),
            ("PAUSED", "Pausado"),
            ("DISABLED", "Deshabilitado"),
            ("IN_APPEAL", "En apelacion"),
            ("UNKNOWN", "Desconocido"),
        ],
        default="UNKNOWN",
        required=True,
    )
    category = fields.Char()
    quality_score = fields.Char()
    body_params_count = fields.Integer(
        string="Parametros body",
        default=0,
        help="Cantidad de variables {{n}} detectadas en el cuerpo del template.",
    )
    is_test_template = fields.Boolean(
        string="Template de prueba",
        default=False,
        help="Marca templates reservados para numeros de prueba de Meta.",
    )

    _sql_constraints = [
        (
            "whatsapp_meta_template_unique",
            "unique(account_id, meta_template_id)",
            "El template de Meta ya fue sincronizado en esta cuenta.",
        )
    ]

    def name_get(self):
        result = []
        for rec in self:
            status_label = dict(self._fields["status"].selection).get(rec.status, rec.status)
            result.append(
                (
                    rec.id,
                    f"{rec.name} ({rec.language_code}) - {status_label}",
                )
            )
        return result
