# Odoo Order Poller — Contexto de Implementación para Claude CLI

## Resumen del Proyecto

Microservicio en Python que hace polling a instancias de Odoo SaaS vía JSON-RPC, extrae órdenes de venta confirmadas (`sale.order`) y las envía como webhooks normalizados a la app de picking StockMaster. El MS es multi-tenant: soporta múltiples conexiones a distintas cuentas de Odoo simultáneamente.

---

## Arquitectura General

```
┌─────────────────────────────────────────────────┐
│              Odoo Poller MS (Python)             │
│                                                  │
│  ┌───────────┐  ┌───────────┐  ┌─────────────┐  │
│  │ TUI       │  │ Scheduler │  │ SQLite DB   │  │
│  │ (Textual) │  │ (asyncio) │  │             │  │
│  │           │  │           │  │ connections │  │
│  │ CRUD de   │  │ Poll cada │  │ sync_log    │  │
│  │ conexiones│  │ 60s por   │  │ retry_queue │  │
│  │           │  │ conexión  │  │             │  │
│  └───────────┘  └─────┬─────┘  └─────────────┘  │
│                       │                          │
│            ┌──────────┴──────────┐               │
│            │                     │               │
│      ┌─────▼─────┐        ┌─────▼─────┐         │
│      │ Odoo A    │        │ Odoo B    │  ...     │
│      │ JSON-RPC  │        │ JSON-RPC  │         │
│      └─────┬─────┘        └─────┬─────┘         │
│            │                     │               │
│            └──────────┬──────────┘               │
│                       │                          │
│              ┌────────▼────────┐                 │
│              │ Transformer     │                 │
│              │ Odoo → Webhook  │                 │
│              │ Payload         │                 │
│              └────────┬────────┘                 │
│                       │                          │
│              ┌────────▼────────┐                 │
│              │ Webhook Sender  │                 │
│              │ POST + retry    │                 │
│              └────────┬────────┘                 │
│                       │                          │
└───────────────────────┼─────────────────────────┘
                        │
                        ▼
        https://stockmaster.notorios.cl/api/webhooks/odoo
```

---

## Stack Tecnológico

- **Lenguaje**: Python 3.12+
- **TUI**: Textual (framework de interfaces en terminal)
- **Base de datos**: SQLite (vía aiosqlite para async)
- **HTTP Client**: httpx (async)
- **JSON-RPC**: Implementación propia sobre httpx (Odoo SaaS usa JSON-RPC 2.0)
- **Scheduler**: asyncio con tareas periódicas por conexión
- **Contenedor**: Docker (deploy en GCP Cloud Run o GCE)
- **Gestión de config**: Variables de entorno + SQLite para conexiones

---

## Conexión a Odoo SaaS — JSON-RPC

Odoo SaaS expone su API externa vía JSON-RPC 2.0 en los siguientes endpoints:

### Autenticación

```
POST https://{odoo_url}/jsonrpc
Content-Type: application/json

{
  "jsonrpc": "2.0",
  "method": "call",
  "params": {
    "service": "common",
    "method": "authenticate",
    "args": ["{db_name}", "{username}", "{api_key}", {}]
  },
  "id": 1
}
```

Retorna un `uid` (integer) que se usa en las llamadas subsiguientes.

### Lectura de Órdenes (search_read)

```
POST https://{odoo_url}/jsonrpc
Content-Type: application/json

{
  "jsonrpc": "2.0",
  "method": "call",
  "params": {
    "service": "object",
    "method": "execute_kw",
    "args": [
      "{db_name}",
      {uid},
      "{api_key}",
      "sale.order",
      "search_read",
      [[
        ["state", "in", ["sale", "done"]],
        ["write_date", ">", "{last_sync_timestamp}"]
      ]],
      {
        "fields": [
          "name", "state", "date_order", "write_date",
          "partner_id", "partner_shipping_id",
          "order_line", "note", "amount_total",
          "client_order_ref"
        ],
        "order": "write_date asc",
        "limit": 100
      }
    ]
  },
  "id": 2
}
```

### Lectura de Líneas de Orden

```
POST https://{odoo_url}/jsonrpc

{
  "jsonrpc": "2.0",
  "method": "call",
  "params": {
    "service": "object",
    "method": "execute_kw",
    "args": [
      "{db_name}", {uid}, "{api_key}",
      "sale.order.line",
      "read",
      [{order_line_ids}],
      {
        "fields": [
          "product_id", "name", "product_uom_qty",
          "price_unit", "price_total",
          "product_template_id"
        ]
      }
    ]
  },
  "id": 3
}
```

### Lectura de Datos del Partner (Cliente + Dirección de Envío)

```
POST https://{odoo_url}/jsonrpc

{
  "jsonrpc": "2.0",
  "method": "call",
  "params": {
    "service": "object",
    "method": "execute_kw",
    "args": [
      "{db_name}", {uid}, "{api_key}",
      "res.partner",
      "read",
      [{partner_ids}],
      {
        "fields": [
          "name", "phone", "mobile", "email",
          "street", "street2", "city", "state_id",
          "zip", "country_id",
          "sale_order_count"
        ]
      }
    ]
  },
  "id": 4
}
```

### Lectura de Datos del Producto (para SKU)

```
POST https://{odoo_url}/jsonrpc

{
  "jsonrpc": "2.0",
  "method": "call",
  "params": {
    "service": "object",
    "method": "execute_kw",
    "args": [
      "{db_name}", {uid}, "{api_key}",
      "product.product",
      "read",
      [{product_ids}],
      {
        "fields": [
          "default_code", "name", "barcode",
          "product_template_attribute_value_ids"
        ]
      }
    ]
  },
  "id": 5
}
```

### Lectura de Variantes del Producto

```
POST https://{odoo_url}/jsonrpc

{
  "jsonrpc": "2.0",
  "method": "call",
  "params": {
    "service": "object",
    "method": "execute_kw",
    "args": [
      "{db_name}", {uid}, "{api_key}",
      "product.template.attribute.value",
      "read",
      [{attribute_value_ids}],
      {
        "fields": ["name", "attribute_id"]
      }
    ]
  },
  "id": 6
}
```

---

## Modelo de Datos — SQLite

### Tabla: `connections`

```sql
CREATE TABLE connections (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
  name TEXT NOT NULL,                          -- Nombre descriptivo (ej. "Le Juste Odoo", "Empresa X")
  odoo_url TEXT NOT NULL,                      -- URL base de Odoo (ej. "https://miempresa.odoo.com")
  odoo_db TEXT NOT NULL,                       -- Nombre de la base de datos
  odoo_username TEXT NOT NULL,                 -- Usuario de API
  odoo_api_key TEXT NOT NULL,                  -- API key de Odoo
  webhook_url TEXT NOT NULL DEFAULT 'https://stockmaster.notorios.cl/api/webhooks/odoo',
  webhook_secret TEXT NOT NULL,                -- Shared secret para autenticación del webhook
  store_id TEXT NOT NULL,                      -- ID de la tienda en StockMaster (para mapeo)
  client_id TEXT NOT NULL,                     -- ID del cliente en StockMaster
  polling_interval_seconds INTEGER NOT NULL DEFAULT 60,
  is_active BOOLEAN NOT NULL DEFAULT 1,        -- Permite pausar/activar conexiones
  last_sync_at TEXT,                           -- ISO 8601 timestamp del último sync exitoso
  last_sync_order_id INTEGER,                  -- Último ID de orden sincronizada
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

### Tabla: `sync_log`

```sql
CREATE TABLE sync_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  connection_id TEXT NOT NULL REFERENCES connections(id),
  synced_at TEXT NOT NULL DEFAULT (datetime('now')),
  orders_found INTEGER NOT NULL DEFAULT 0,
  orders_sent INTEGER NOT NULL DEFAULT 0,
  orders_failed INTEGER NOT NULL DEFAULT 0,
  error_message TEXT,                          -- NULL si éxito
  duration_ms INTEGER                          -- Duración del ciclo de polling
);
```

### Tabla: `retry_queue`

```sql
CREATE TABLE retry_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  connection_id TEXT NOT NULL REFERENCES connections(id),
  odoo_order_id INTEGER NOT NULL,              -- ID de la orden en Odoo
  external_id TEXT NOT NULL,                   -- ID compuesto: odoo_{db}_{order_id}
  payload TEXT NOT NULL,                       -- JSON del webhook payload completo
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 5,
  next_retry_at TEXT NOT NULL,                 -- ISO 8601, se calcula con backoff exponencial
  last_error TEXT,
  status TEXT NOT NULL DEFAULT 'PENDING',      -- PENDING | SUCCESS | FAILED
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(connection_id, odoo_order_id)         -- Idempotencia: no duplicar órdenes
);
```

### Tabla: `sent_orders` (tracking de idempotencia)

```sql
CREATE TABLE sent_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  connection_id TEXT NOT NULL REFERENCES connections(id),
  odoo_order_id INTEGER NOT NULL,
  external_id TEXT NOT NULL,
  sent_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(connection_id, odoo_order_id)
);
```

---

## Webhook Payload — Formato de Salida

El payload que envía el poller al endpoint de StockMaster debe contener todos los datos necesarios para crear una orden en la app de picking. El endpoint es:

```
POST https://stockmaster.notorios.cl/api/webhooks/odoo
Headers:
  Content-Type: application/json
  X-Webhook-Secret: {webhook_secret_de_la_conexion}
  X-Odoo-Connection-Id: {connection_id}
```

### Estructura del Payload

```json
{
  "event": "order.confirmed",
  "external_id": "odoo_{db_name}_{order_id}",
  "source": {
    "platform": "ODOO",
    "connection_id": "abc123",
    "store_id": "store_xyz",
    "client_id": "client_xyz"
  },
  "order": {
    "platform_order_id": "42",
    "platform_order_number": "S00042",
    "date_order": "2025-01-15T10:30:00Z",
    "financial_status": "paid",
    "note": "Nota interna de la orden en Odoo",
    "client_order_ref": "REF-CLIENTE-001",
    "amount_total": 5990000,
    "tags": [],
    "platform_attributes": {
      "odoo_state": "sale",
      "client_order_ref": "REF-CLIENTE-001"
    }
  },
  "customer": {
    "name": "Juan Pérez",
    "phone": "+56912345678",
    "email": "juan@example.com",
    "orders_count": 5
  },
  "shipping_address": {
    "name": "Juan Pérez",
    "address1": "Av. Providencia 1234",
    "address2": "Depto 502",
    "city": "Providencia",
    "province": "Región Metropolitana",
    "zip": "7500000",
    "country": "CL",
    "phone": "+56912345678"
  },
  "items": [
    {
      "sku": "PROD-001",
      "name": "Perfume El Testigo - Edición Limitada",
      "variant_name": "50ml",
      "quantity": 2,
      "price_cents": 2995000
    }
  ]
}
```

### Mapeo Odoo → Payload

| Campo Payload | Origen en Odoo | Notas |
|---|---|---|
| `order.platform_order_id` | `sale.order.id` | ID interno de Odoo |
| `order.platform_order_number` | `sale.order.name` | Ej. "S00042" |
| `order.date_order` | `sale.order.date_order` | Convertir a ISO 8601 UTC |
| `order.financial_status` | Siempre `"paid"` | Solo sincronizamos órdenes confirmadas |
| `order.note` | `sale.order.note` | Notas internas |
| `order.client_order_ref` | `sale.order.client_order_ref` | Referencia del cliente |
| `order.amount_total` | `sale.order.amount_total` | Convertir a centavos (×100) |
| `customer.name` | `res.partner.name` (de `partner_id`) | Partner principal |
| `customer.phone` | `res.partner.phone` o `mobile` | Preferir mobile, fallback a phone |
| `customer.email` | `res.partner.email` | — |
| `customer.orders_count` | `res.partner.sale_order_count` | Total de órdenes del partner |
| `shipping_address.*` | `res.partner` (de `partner_shipping_id`) | Partner de envío |
| `shipping_address.province` | `res.partner.state_id.name` | Resolver nombre del state |
| `shipping_address.country` | `res.partner.country_id.code` | Código ISO del país |
| `items[].sku` | `product.product.default_code` | Si no tiene, generar: `ODOO-{db}-{product_id}` |
| `items[].name` | `sale.order.line.name` o `product.product.name` | — |
| `items[].variant_name` | `product.template.attribute.value` concatenados | Ej. "Color: Rojo, Talla: L" |
| `items[].quantity` | `sale.order.line.product_uom_qty` | — |
| `items[].price_cents` | `sale.order.line.price_unit` | Convertir a centavos (×100) |

### Lógica de Ingesta de Items (equivalente a Shopify)

- Si Odoo no provee `default_code` (SKU), auto-generar: `ODOO-{db_name}-{product_id}`
- Items con cantidad 0 se filtran
- Precio se convierte a centavos (integer): `int(price_unit * 100)`
- Variantes se construyen concatenando los `product.template.attribute.value` asociados al producto

---

## Lógica del Poller (Ciclo de Polling)

### Flujo por Conexión

```
1. Autenticar con Odoo (obtener uid) — cachear sesión
2. Consultar sale.order donde:
   - state IN ('sale', 'done')
   - write_date > last_sync_at de la conexión
   - ORDER BY write_date ASC
   - LIMIT 100
3. Para cada orden nueva:
   a. Verificar en sent_orders si ya fue enviada (idempotencia)
   b. Si ya fue enviada, skip
   c. Obtener datos relacionados:
      - Partner (cliente) vía partner_id
      - Partner de envío vía partner_shipping_id
      - Líneas de orden vía order_line
      - Productos vía product_id de cada línea
      - Variantes vía product_template_attribute_value_ids
   d. Transformar a webhook payload
   e. Enviar webhook POST
   f. Si éxito: registrar en sent_orders, actualizar last_sync_at
   g. Si fallo: agregar a retry_queue con next_retry_at
4. Registrar en sync_log el resultado del ciclo
5. Procesar retry_queue:
   - Buscar items con next_retry_at <= now() AND status = 'PENDING'
   - Reintentar envío
   - Si éxito: marcar SUCCESS, agregar a sent_orders
   - Si fallo: incrementar attempts, calcular nuevo next_retry_at
   - Si attempts >= max_attempts: marcar FAILED
```

### Backoff Exponencial para Retries

```python
def calculate_next_retry(attempts: int) -> datetime:
    """Backoff: 30s, 1min, 2min, 4min, 8min"""
    base_seconds = 30
    delay = base_seconds * (2 ** attempts)
    max_delay = 600  # 10 minutos máximo
    return datetime.utcnow() + timedelta(seconds=min(delay, max_delay))
```

### Optimización de Queries

Para evitar hacer N+1 queries a Odoo por cada orden, agrupar las lecturas relacionadas:

1. Traer todas las órdenes del batch
2. Recolectar todos los `partner_id`, `partner_shipping_id`, `order_line` IDs
3. Hacer una sola lectura masiva de partners
4. Hacer una sola lectura masiva de order lines
5. De las order lines, recolectar todos los `product_id`
6. Hacer una sola lectura masiva de productos
7. De los productos, recolectar todos los `product_template_attribute_value_ids`
8. Hacer una sola lectura masiva de attribute values
9. Ensamblar los payloads con los datos ya en memoria

---

## TUI — Interfaz de Consola con Textual

### Pantallas / Vistas

#### 1. Dashboard Principal
- Lista de conexiones con estado (activa/pausada)
- Indicadores por conexión:
  - Última sincronización (hace cuánto tiempo)
  - Órdenes enviadas hoy
  - Items en retry queue
  - Errores recientes
- Acciones: Crear conexión, Ver logs, Iniciar/Detener poller

#### 2. CRUD de Conexiones
- **Crear**: Formulario con campos:
  - Nombre (descriptivo)
  - URL de Odoo
  - Base de datos
  - Usuario
  - API Key (input oculto)
  - Store ID (StockMaster)
  - Client ID (StockMaster)
  - Webhook URL (pre-llenado con default)
  - Webhook Secret (auto-generado, editable)
  - Intervalo de polling (default 60s)
- **Editar**: Mismo formulario, pre-llenado
- **Eliminar**: Con confirmación
- **Test**: Botón para probar conexión a Odoo (authenticate) y webhook (envío de test ping)
- **Activar/Pausar**: Toggle rápido

#### 3. Vista de Logs
- Tabla con últimos N sync_logs por conexión
- Filtrable por conexión
- Muestra: timestamp, órdenes encontradas/enviadas/fallidas, errores

#### 4. Vista de Retry Queue
- Lista de items pendientes de reintento
- Acciones: Reintentar ahora, Descartar, Ver payload
- Filtrable por conexión y estado

### Layout Sugerido

```
┌──────────────────────────────────────────────────┐
│  🔄 Odoo Order Poller          [Poller: RUNNING] │
├──────────────────────────────────────────────────┤
│  Connections                                      │
│  ┌────────────────────────────────────────────┐  │
│  │ ● Le Juste Odoo    | Last: 30s ago | 12 ✓ │  │
│  │ ○ Empresa X        | Paused        |  0   │  │
│  │ ● Empresa Y        | Last: 45s ago |  3 ✓ │  │
│  └────────────────────────────────────────────┘  │
│                                                   │
│  [+New] [Edit] [Delete] [Test] [Logs] [Retries]  │
│                                                   │
│  ─── Recent Activity ───                          │
│  10:30:15 | Le Juste    | 3 orders sent           │
│  10:29:12 | Empresa Y   | 1 order sent            │
│  10:28:45 | Le Juste    | 0 new orders             │
│  10:27:30 | Le Juste    | ⚠ 1 failed, queued      │
│                                                   │
│  Retry Queue: 2 pending | 0 failed                │
└──────────────────────────────────────────────────┘
```

---

## Estructura del Proyecto

```
odoo-poller/
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── README.md
├── .env.example
├── src/
│   ├── __init__.py
│   ├── main.py                    # Entry point, arranca TUI + scheduler
│   ├── config.py                  # Configuración desde env vars
│   ├── db/
│   │   ├── __init__.py
│   │   ├── database.py            # Init SQLite, migraciones
│   │   ├── models.py              # Dataclasses para Connection, SyncLog, RetryItem
│   │   └── repositories.py        # CRUD operations async
│   ├── odoo/
│   │   ├── __init__.py
│   │   ├── client.py              # JSON-RPC client async
│   │   └── mapper.py              # Odoo data → webhook payload transformer
│   ├── poller/
│   │   ├── __init__.py
│   │   ├── scheduler.py           # Async scheduler, gestiona tareas por conexión
│   │   ├── worker.py              # Lógica de un ciclo de polling
│   │   └── sender.py              # Webhook sender con retry logic
│   └── tui/
│       ├── __init__.py
│       ├── app.py                 # Textual App principal
│       ├── screens/
│       │   ├── __init__.py
│       │   ├── dashboard.py       # Pantalla principal
│       │   ├── connection_form.py # Crear/Editar conexión
│       │   ├── logs.py            # Vista de sync logs
│       │   └── retries.py         # Vista de retry queue
│       └── widgets/
│           ├── __init__.py
│           ├── connection_list.py # Widget de lista de conexiones
│           └── activity_log.py    # Widget de actividad reciente
├── tests/
│   ├── test_odoo_client.py
│   ├── test_mapper.py
│   ├── test_worker.py
│   └── test_sender.py
└── data/
    └── .gitkeep                   # SQLite DB se crea aquí en runtime
```

---

## Ejecución

### Desarrollo Local

```bash
# Instalar dependencias
pip install -e ".[dev]"

# Ejecutar
python -m src.main

# O con variables de entorno
POLLER_DB_PATH=./data/poller.db python -m src.main
```

### Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .
COPY src/ src/
VOLUME /app/data
CMD ["python", "-m", "src.main"]
```

```bash
docker build -t odoo-poller .
docker run -it -v $(pwd)/data:/app/data odoo-poller
```

### Variables de Entorno

```
POLLER_DB_PATH=./data/poller.db          # Ruta al archivo SQLite
POLLER_LOG_LEVEL=INFO                     # DEBUG | INFO | WARNING | ERROR
POLLER_DEFAULT_WEBHOOK_URL=https://stockmaster.notorios.cl/api/webhooks/odoo
```

---

## Consideraciones de Implementación

### Concurrencia
- Usar `asyncio` para manejar múltiples conexiones de forma concurrente
- Cada conexión activa tiene su propia tarea async que se ejecuta cada N segundos
- `aiosqlite` para acceso no-bloqueante a SQLite
- `httpx.AsyncClient` para llamadas a Odoo y webhooks

### Idempotencia
- Clave de idempotencia: `external_id` = `odoo_{db_name}_{sale_order_id}`
- Antes de enviar, verificar en `sent_orders` si ya fue procesada
- Si la orden ya fue enviada, skip silencioso
- UNIQUE constraint en `(connection_id, odoo_order_id)` como safety net

### Manejo de Sesión Odoo
- Cachear el `uid` por conexión para no re-autenticar en cada ciclo
- Si una llamada falla con error de autenticación, re-autenticar y reintentar
- Timeout de 30s por llamada JSON-RPC

### Rate Limiting
- Odoo SaaS puede tener rate limits
- Implementar un delay configurable entre llamadas por conexión
- Si se recibe HTTP 429, respetar Retry-After header

### Graceful Shutdown
- Capturar SIGTERM/SIGINT
- Completar ciclo de polling en progreso
- Cerrar conexiones de DB y HTTP limpiamente
- Importante para Docker/Cloud Run

### Logging
- Logging estructurado a stdout (para GCP Cloud Logging)
- Nivel configurable por variable de entorno
- Incluir connection_id en cada log entry para filtrado

---

## Dependencias Python

```toml
[project]
name = "odoo-poller"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "textual>=0.80.0",
    "httpx>=0.27.0",
    "aiosqlite>=0.20.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
]
```

---

## Schema de Referencia — App StockMaster (Destino)

### Tabla `orders` — Campos Relevantes para Ingesta

| Campo | Tipo | Descripción |
|---|---|---|
| `platform_order_id` | TEXT | ID externo (Odoo order ID como string) |
| `platform_order_number` | TEXT | Número visible (ej. "S00042") |
| `platform` | TEXT | "ODOO" |
| `store_id` | TEXT | ID de tienda en StockMaster |
| `client_id` | TEXT | ID de cliente/empresa en StockMaster |
| `status` | TEXT | Siempre inicia en "PENDING" |
| `customer_name` | TEXT | Nombre del cliente |
| `customer_phone` | TEXT | Teléfono del cliente |
| `customer_orders_count` | INTEGER | Cantidad de órdenes previas |
| `shipping_address_raw` | JSON | Dirección de envío completa |
| `tags` | ARRAY | Tags (vacío desde Odoo por defecto) |
| `client_note` | TEXT | Nota de la orden |
| `platform_attributes` | JSON | Metadata adicional de Odoo |
| `financial_status` | TEXT | "paid" |

### Tabla `order_items` — Campos Relevantes

| Campo | Tipo | Descripción |
|---|---|---|
| `sku` | TEXT | SKU del producto |
| `name` | TEXT | Nombre del producto |
| `variant_name` | TEXT | Nombre de variante |
| `quantity` | INTEGER | Cantidad a pickear |
| `picked_quantity` | INTEGER | Inicia en 0 |

### Lógica de Items

- Si Odoo no provee SKU (`default_code`), auto-generar: `ODOO-{db_name}-{product_id}`
- Items con cantidad 0 se filtran
- Precio se convierte a centavos: `int(price_unit * 100)`

---

## Notas Adicionales

- El MS debe ser robusto ante caídas de Odoo: si una conexión falla, no debe afectar las demás
- El TUI debe seguir siendo interactivo mientras el poller corre en background
- La DB SQLite se almacena en un volumen persistente en Docker
- El webhook secret se genera automáticamente (UUID4) al crear una conexión, pero es editable
- El endpoint de StockMaster aún no está implementado para Odoo, la validación del webhook se hará posteriormente en la app
