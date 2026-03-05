{
    # Nombre visible en Apps.
    "name": "WhatsApp Meta Order Simple",
    # Descripcion corta del objetivo del addon.
    "summary": "Integracion simple de Meta WhatsApp para pedidos",
    # Version del modulo (Odoo 18).
    "version": "18.0.1.0.0",
    "category": "Sales",
    "author": "Hernan",
    "license": "LGPL-3",
    # Dependencias minimas para envio desde sale.order y chatter.
    "depends": ["sale_management", "mail"],
    # Archivos cargados al instalar/actualizar.
    "data": [
        "security/ir.model.access.csv",
        "views/whatsapp_meta_account_views.xml",
        "views/sale_order_views.xml",
        "views/whatsapp_send_wizard_views.xml",
        "views/whatsapp_test_send_wizard_views.xml",
    ],
    "installable": True,
    "application": False,
}
