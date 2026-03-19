# 📄 Documento de Arquitectura: Lectura MQTT → Base de Datos (Supabase)

## 🧩 Objetivo
Entregarle a *lovable* una descripción clara de la **arquitectura completa** necesaria para:
1. **Suscribirse al canal MQTT** donde publica la Raspberry (telemetría de somnolencia).
2. **Procesar/parsear** los mensajes entrantes.
3. **Persistirlos en la base de datos** (Supabase) según el esquema ya definido.

Este backend (o servicio) será el responsable de alimentar el panel administrativo que se muestra en la foto, entregando:
- Conductores/vehículos en línea
- Alertas del día
- Tiempo de operación
- Historial de sesiones / eventos / emergencias

---

## 🧭 Contexto actual (repositorio)

El proyecto ya contiene:
- Un **publicador MQTT** en `main.py` + `output/mqttpublisher.py` que emite telemetría **desde la Raspberry (cliente)**.
- Un **esquema de BD Supabase** en `supabase_setup.sql` con tablas listadas para sesiones, métricas, eventos, emergencias y telemetría cruda.
- Un utilitario `storage/supabasesync.py` que puede escribir en Supabase usando `supabase-py`, pero actualmente no está siendo alimentado con datos.
- Documentos de configuración (`CONFIG_PANEL_ADMINISTRATIVO.txt`, `PLANTILLA_MQTT_SUPABASE_DB.txt`) con variables de ambiente esperadas.

---

## 📡 1) MQTT: tópico & payload esperado

### 🧷 Tópico MQTT (configurable)
Se usa el tópico configurado en `.env` (variable `MQTT_TOPIC`). En el proyecto actual el valor por defecto es:

```
fleet/{vehicle_id}/telemetry
```

Ejemplo real (de CONFIG_PANEL_ADMINISTRATIVO.txt):
```
fleet/truck_042/telemetry
```

### 🧱 Payload (JSON) que publica la Raspberry
Derivado del código en `main.py` (estructura del objeto `telemetry`):

```json
{
  "v": "vehicle_id",
  "d": "driver_id",
  "ts": 167xxxxxxx,
  "session_id": "ses_...",
  "score": {
    "fatigue_score": 42,
    "level": 2,
    "reasons": ["..."],
    ...
  },
  "alerts": { "active": true, "level": 2, "reasons": [...] },
  "emergency": { "emergencyflag": true, "emergencytype": "..." },
  "sys": { "fps": 12.3, "status": "online" }
}
```

> Nota: el payload puede evolucionar (se agregan parámetros). El backend debe ser tolerante y guardar *todo* el JSON (telemetría cruda) para posibles consultas posteriores.

---

## 🗄️ 2) Base de datos Supabase: esquema y tablas

El archivo `supabase_setup.sql` define las tablas necesarias. Debe ejecutarse en Supabase antes de iniciar el sistema.

### 🧩 Tablas principales (básicas para el dashboard):

| Tabla | Propósito | Uso en backend | Nota clave |
|------|----------|---------------|-----------|
| `public.sessions` | Registro de cada sesión de conducción | Se crea/actualiza cuando inicia/cierra sesión | `session_id` es clave principal |
| `public.metrics_summary` | Resumen por minuto de métricas (fatiga, PERCLOS, EAR, MAR, etc.) | Se ingresa en bloque (cada cierto tiempo) | Tiene campo `payload jsonb` con datos crudos |
| `public.events` | Eventos detectados (parpadeo prolongado, micro-oscilaciones, etc.) | Insertar cuando detecto un evento relevante | Puede usar `param_id`, `event_type`
| `public.emergency_alerts` | Alertas médicas / emergencias | Insertar cada vez que se detecta emergencia (por ejemplo, ojos cerrados > 2s) | Soporta resolución (para cuando termina)
| `public.telemetry_raw` | Guardar TODO el JSON recibido desde MQTT | Se inserta *cada* mensaje MQTT (raw) | Ideal para debug y consultas ad-hoc

> Importante: el frontend usa **Supabase Anon Key** para consultas en lectura, mientras que el backend debe escribir usando **Supabase Service Role Key**.

---

## 🛠️ 3) Requerimientos funcionales para el backend

### A) Consumir MQTT (suscripción)
- Suscribirse (QoS 0/1/2 según configuración) a:
  - `fleet/+/telemetry` (comodín para múltiples vehículos)
- Reconexión automática ante caídas de broker.
- Registrar reconexiones/fallos (logs printf o logger).

### B) Validación mínima y parsing
- Asegurarse de que el payload sea JSON válido.
- Extraer al menos:
  - `vehicle_id` (campo `v` o desde el tópico)
  - `driver_id` (`d`)
  - `session_id`
  - `ts` (timestamp de la telemetría)
- Asegurar que no haya pérdida de datos críticos (si falta algo, guardarlo igualmente en `telemetry_raw` con bandera de anomalía).

### C) Persistencia en Supabase
- **Siempre** insertar el mensaje crudo en `public.telemetry_raw`.
- **(Opcional)** crear/actualizar la sesión en `public.sessions` según mensajes recibidos:
  - Al recibir el primer mensaje de una `session_id` nueva: crear `sessions` con `start_time`.
  - En cada mensaje, actualizar `max_fatigue` / `alert_count` / `updated_at`.
- **(Opcional/edad)**: generar resúmenes/minuto y escribir en `public.metrics_summary` con agregados.
- **(Opcional)**: cuando el payload incluya `alerts` / `emergency`, crear registros en `public.events` / `public.emergency_alerts`.

> Recomendación inicial mínima (MVP):
> 1) Guardar todo en `telemetry_raw`.
> 2) Crear/actualizar `sessions` para poder visualizar "conductores en línea".
> 3) Generar `events` / `metrics_summary` si se quiere un dashboard más avanzado.

---

## 🔐 4) Variables de entorno esperadas

El backend debe usar un `.env` parecido a este (puede ser el mismo que usa la Raspberry, o uno dedicado al backend):

```env
# MQTT
EMQX_HOST=hbc0fc94.ala.us-east-1.emqxsl.com
EMQX_PORT=8883
EMQX_USERNAME=Cristian
EMQX_PASSWORD=Noviembre0824@
EMQX_TLS=true
MQTT_TOPIC=fleet/{vehicle_id}/telemetry
MQTT_QOS=1
MQTT_CLIENT_ID=somnoalert-backend-01
MQTT_CA_CERT_PATH=path/to/ca.crt

# SUPABASE
SUPABASE_URL=https://<your-project>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=sb_secret_xxx
SUPABASE_ANON_KEY=sb_publishable_xxx   # solo para front-end, no en backend
SUPABASE_SCHEMA=public
```

---

## 📌 5) Checklist para el desarrollo (lo que *lovable* debe entregar)

✅ Implementar un servicio/daemon que:
- [ ] Se suscribe a `fleet/+/telemetry` y consume mensajes MQTT.
- [ ] Valida/parsa el JSON, lo inserta en `telemetry_raw`.
- [ ] Mantiene `sessions` actualizada (online / offline, alertas, max fatigue, timestamps).
- [ ] (Opcional) Genera `metrics_summary` y `events` / `emergency_alerts` si se requiere para el dashboard.
- [ ] Maneja reconexiones automáticas y errores de red.
- [ ] Es configurable vía `.env`.

✅ Adicional (ideal para dashboard “tiempo real”):
- [ ] Publica un evento en una tabla o servicio de notificación cuando se genere una emergencia.
- [ ] Expone un endpoint mínimo (o webhooks) para el frontend (si el dashboard no hace polling directo a Supabase).

---

## 🧪 Qué validar en el dashboard actual (foto)

El dashboard muestra:
- **Estado MQTT** (conectado + mensajes)
- **Conductores en línea** (suponemos es `sessions` activas)
- **Alertas del día** (tablas `events` / `emergency_alerts`)
- **Tiempo de operación** (puede calcularse desde `sessions.start_time` hasta ahora)

El backend debe proveer datos consistentes para alimentar esos widgets.

---

## 🧩 Siguientes pasos sugeridos
1. Levantar un servicio mínimo que consuma MQTT y escriba en `telemetry_raw`.
2. Verificar en Supabase que entran datos.
3. Conectar el frontend/dashboard para leer esa tabla y validar que los datos se reflejan.
4. Implementar el resto de tablas (sessions, events, emergency_alerts, metrics_summary).

---

📌 **Nota final**: este documento asume que la Raspberry publica a MQTT y que el dashboard lee desde Supabase. Si el dashboard usa directamente MQTT (WS), hay que adaptar el flujo, pero el backend seguirá siendo la fuente de verdad para persistencia.
