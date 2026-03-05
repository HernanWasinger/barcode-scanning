# -*- coding: utf-8 -*-
"""
Registro de modelos del módulo whatsapp_meta_order_simple.

Orden de carga (importante por dependencias):
1. sale_order: extiende sale.order
2. whatsapp_meta_account: cuenta, logs, mensajes, templates
3. whatsapp_send_wizard: wizard para enviar desde orden
4. whatsapp_test_send_wizard: wizard para pruebas
"""

# Registra extensiones de negocio del addon.
from . import sale_order
from . import whatsapp_meta_account
from . import whatsapp_send_wizard
from . import whatsapp_test_send_wizard
