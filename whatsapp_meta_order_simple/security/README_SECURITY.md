# Permisos de seguridad (ir.model.access.csv)

Este archivo define quién puede leer/escribir/crear/eliminar cada modelo.

| Modelo | Grupo | Lectura | Escritura | Crear | Eliminar |
|--------|-------|---------|------------|-------|----------|
| whatsapp.meta.account | Administradores | ✓ | ✓ | ✓ | ✓ |
| whatsapp.meta.log | Administradores | ✓ | - | - | - |
| whatsapp.meta.message | Administradores | ✓ | ✓ | ✓ | - |
| whatsapp.meta.template | Administradores | ✓ | ✓ | ✓ | ✓ |
| whatsapp.send.wizard | Usuarios | ✓ | ✓ | ✓ | ✓ |
| whatsapp.test.send.wizard | Usuarios | ✓ | ✓ | ✓ | ✓ |

- **base.group_system**: administradores (configuración de cuenta, tokens)
- **base.group_user**: usuarios normales (enviar desde pedidos, pruebas)
