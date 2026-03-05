# -*- coding: utf-8 -*-
"""
Registro de controladores HTTP del módulo.

Expone el endpoint /whatsapp/meta/webhook para:
- Handshake de verificación (GET)
- Recepción de eventos de mensajes (POST)
"""

# Registra endpoints publicos del modulo.
from . import webhook
