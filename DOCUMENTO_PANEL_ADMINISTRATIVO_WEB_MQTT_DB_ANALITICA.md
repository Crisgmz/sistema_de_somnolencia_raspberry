# SomnoAlert - Integracion del Panel Administrativo Web

## Objetivo

Este documento resume los cambios recientes del sistema y define como debe conectarse el sistema administrativo web para leer datos en tiempo real por MQTT, consultar datos historicos en base de datos y construir analisis sobre fatiga, alertas, eventos y emergencias.

La idea central es:

- MQTT = canal de tiempo real.
- Supabase/Postgres = fuente historica y operativa para reportes.
- SQLite local en Raspberry = cola y estado resiliente ante reinicios o falta de internet.

## Cambios Recientes del Sistema

### 1. MQTT corregido con WebSockets

El sistema ahora respeta estas variables:

```env
EMQX_HOST=hbc0fc94.ala.us-east-1.emqxsl.com
EMQX_PORT=8084
EMQX_TLS=true
MQTT_TRANSPORT=websockets
MQTT_WS_PATH=/mqtt
MQTT_TOPIC=test/connection
MQTT_QOS=1
MQTT_CLIENT_ID=raspi-somnoalert-001
```

Antes el codigo conectaba como MQTT TCP normal aunque el `.env` estaba en WebSockets. Ya se agrego soporte real para `MQTT_TRANSPORT=websockets` y `MQTT_WS_PATH=/mqtt`.

Prueba validada:

```bash
python test_mqtt_connection.py
```

Salida esperada:

```text
[OK] MQTT publicado en topic=test/connection transport=websockets
```

### 2. Score persistente

El `fatigue_score` ya no se reinicia al reiniciar el proceso. Ahora se guarda en SQLite en una tabla local `score_state`, asociada a `vehicle_id + driver_id`.

Al arrancar el sistema, si existe estado previo, se restaura:

```text
[SCORE] Estado restaurado score=42 max=55 alertas=3
```

Esto permite que el panel no vea caidas falsas del score por reinicio de la Raspberry.

### 3. Eventos historicos para reglas

El sistema ahora guarda eventos activos en SQLite local, tabla `event_history`. El `RuleEngine` puede evaluar ventanas de tiempo usando datos persistidos, no solo memoria RAM.

Esto ayuda a mantener estados como:

- Eventos acumulados en 5 minutos.
- Cruces de PERCLOS + cierre prolongado.
- Monotonia / tiempo en tarea.
- Alertas por cluster.

### 4. Menos sensibilidad y sonido con retardo

Se redujo la sensibilidad de ojos, boca, cabeza, manos y contexto.

Cambios relevantes:

- Emergencia por ojos cerrados: ahora exige 2 segundos.
- Cabeza abajo: umbral mas estricto.
- Buzzer: no suena inmediatamente para alertas normales; espera 2 segundos de estado sostenido.
- Eventos sostenidos ya no suman score en cada frame, solo al entrar al evento.

## Arquitectura Recomendada

```text
Raspberry Pi
  |
  | MQTT WebSocket/TLS
  v
EMQX Cloud
  |
  | suscripcion tiempo real
  v
Backend / Servicio Administrativo
  |
  | inserts/upserts
  v
Supabase/Postgres
  |
  | consultas, realtime, reportes
  v
Panel Administrativo Web
```

El panel web puede usar dos fuentes:

1. MQTT directo por WebSocket para monitoreo instantaneo.
2. Supabase para datos historicos, dashboard, filtros y analitica.

Para produccion, se recomienda que el frontend lea principalmente de Supabase y que un backend seguro sea quien consuma MQTT y escriba en la base de datos. Si el panel se conecta directo a MQTT, no debe exponer credenciales sensibles en un frontend publico.

## Flujo MQTT

### Publicador

La Raspberry publica telemetria desde `output/mqttpublisher.py`.

Topic actual de prueba:

```text
test/connection
```

Topic recomendado para flota:

```text
fleet/{vehicle_id}/telemetry
```

Ejemplo:

```text
fleet/truck_042/telemetry
```

Topic de supervisor si no se configura otro:

```text
<MQTT_TOPIC>/supervisor
```

Ejemplo:

```text
fleet/truck_042/telemetry/supervisor
```

### Suscripcion del sistema administrativo

Para una flota:

```text
fleet/+/telemetry
```

Para supervisor:

```text
fleet/+/telemetry/supervisor
```

Para el estado actual de prueba:

```text
test/connection
test/connection/supervisor
```

### Payload principal

La Raspberry envia JSON con esta forma general:

```json
{
  "v": "vehicle_id",
  "d": "driver_id",
  "ts": 1710000000,
  "session_id": "ses_abc123",
  "score": {
    "fatigue_score": 42,
    "level": 2,
    "label": "SOMNOLENCIA",
    "reasons": ["EYE_CLOSED_MS"],
    "max_fatigue": 55,
    "alert_count": 7
  },
  "alerts": {
    "active": true,
    "level": 2,
    "reasons": ["EYE_CLOSED_MS"]
  },
  "emergency": {
    "emergencyflag": false,
    "emergencytype": null,
    "reasons": [],
    "fixedbuzzer": false,
    "active": false,
    "type": null
  },
  "alert_memory": {
    "active_level": 2,
    "active_reasons": ["EYE_CLOSED_MS"],
    "active_duration_s": 3.4,
    "peaks": { "5m": 2, "15m": 3, "60m": 3 },
    "emergency_counts": { "5m": 0, "15m": 1, "60m": 1 }
  },
  "sys": {
    "fps": 14.8,
    "status": "online",
    "mqtt": {
      "connected": true,
      "transport": "websockets",
      "published_count": 120,
      "delivered_count": 119,
      "dropped_count": 0
    },
    "supabase": {
      "enabled": true,
      "pending": 0,
      "flushed": 300,
      "failed": 0
    },
    "calibrated": true
  }
}
```

El backend debe guardar el JSON completo aunque falten campos, porque los payloads pueden evolucionar.

## Flujo Base de Datos

El archivo base es:

```text
supabase_setup.sql
```

Tablas principales:

| Tabla | Uso |
| --- | --- |
| `sessions` | Sesiones de conduccion por `session_id`. |
| `telemetry_raw` | JSON crudo recibido por MQTT o enviado por la Raspberry. |
| `metrics_summary` | Resumen por minuto: EAR, MAR, PERCLOS, score, nivel, iluminacion, monotonia. |
| `events` | Eventos de parametros: ojos, boca, cabeza, manos, contexto. |
| `emergency_alerts` | Emergencias medicas o criticas. |

### Que escribe la Raspberry

La Raspberry ya tiene sincronizacion local a Supabase mediante `storage/supabasesync.py`.

Escribe:

- `sessions`
- `telemetry_raw`
- `metrics_summary`
- `events`
- `emergency_alerts`

Si no hay red, guarda en SQLite local y sincroniza despues.

### Que debe leer el panel web

Para dashboard en vivo:

```sql
select *
from telemetry_raw
order by ts desc
limit 100;
```

Para sesiones activas:

```sql
select *
from sessions
where end_time is null
order by updated_at desc;
```

Para ultimos scores por vehiculo:

```sql
select distinct on (vehicle_id)
  vehicle_id,
  driver_id,
  session_id,
  ts,
  payload->'score' as score,
  payload->'alerts' as alerts,
  payload->'emergency' as emergency
from telemetry_raw
order by vehicle_id, ts desc;
```

Para alertas del dia:

```sql
select *
from events
where ts >= date_trunc('day', now())
order by ts desc;
```

Para emergencias del dia:

```sql
select *
from emergency_alerts
where ts >= date_trunc('day', now())
order by ts desc;
```

Para resumen por conductor:

```sql
select
  driver_id,
  count(*) as sesiones,
  max(max_fatigue) as max_fatigue,
  sum(alert_count) as alertas
from sessions
where start_time >= now() - interval '7 days'
group by driver_id
order by max_fatigue desc;
```

## Componentes del Panel Administrativo

### 1. Monitor en tiempo real

Fuente:

- MQTT directo o `telemetry_raw` con Supabase Realtime.

Mostrar:

- Vehiculo.
- Conductor.
- Estado online/offline.
- Score actual.
- Nivel actual.
- Motivos activos.
- FPS/camara.
- Estado MQTT y estado Supabase.
- Ultimo mensaje recibido.

Regla de online:

```text
online = ultimo ts recibido hace menos de 20 segundos
```

### 2. Vista de alertas

Fuente:

- `events`
- `emergency_alerts`
- `telemetry_raw.payload->alerts`

Mostrar:

- Tipo de evento.
- Severidad.
- Valor del parametro.
- Score al momento.
- Vehiculo/conductor.
- Hora.
- Duracion si aplica.

### 3. Vista de sesiones

Fuente:

- `sessions`
- `metrics_summary`

Mostrar:

- Inicio.
- Fin.
- Duracion.
- Max score.
- Alertas totales.
- Promedio de PERCLOS.
- Fatiga promedio.
- Emergencias asociadas.

### 4. Perfil de conductor

Fuente:

- `sessions`
- `metrics_summary`
- `events`
- `emergency_alerts`

Analisis utiles:

- Score maximo por dia.
- Promedio de fatiga por turno.
- Momentos del dia con mas alertas.
- Parametros mas frecuentes.
- Eventos por hora de conduccion.
- Tendencia de fatiga semanal.

### 5. Analitica de flota

Indicadores recomendados:

- Vehiculos online.
- Conductores activos.
- Alertas ultimas 24 horas.
- Emergencias ultimas 24 horas.
- Top 5 conductores por riesgo.
- Top 5 vehiculos por alertas.
- Horas mas criticas del dia.
- Promedio de fatiga por ruta/turno si luego se agrega ruta.

## Analisis Recomendados

### Nivel de riesgo actual

Usar:

- `score.level`
- `score.fatigue_score`
- `alerts.active`
- `emergency.emergencyflag`
- `alert_memory.peaks.5m`

Regla sugerida:

```text
NORMAL: level = 0 y sin alertas
BAJO: level = 1
MEDIO: level = 2
ALTO: level = 3
CRITICO: level = 4 o emergencyflag = true
```

### Fatiga sostenida

Consultar `metrics_summary`:

```sql
select
  session_id,
  avg(fatigue_score) as avg_score,
  max(fatigue_score) as max_score,
  avg(perclos) as avg_perclos
from metrics_summary
where ts >= now() - interval '30 minutes'
group by session_id;
```

### Eventos frecuentes por parametro

```sql
select
  event_type,
  count(*) as total,
  avg(fatigue_score) as avg_score
from events
where ts >= now() - interval '7 days'
group by event_type
order by total desc;
```

### Deteccion de conductor de alto riesgo

Criterios sugeridos:

- 3 o mas alertas nivel 3 en 24 horas.
- 1 o mas emergencias en 24 horas.
- `max_fatigue >= 80`.
- PERCLOS promedio alto en los ultimos 30 minutos.

Consulta base:

```sql
select
  s.driver_id,
  max(s.max_fatigue) as max_fatigue,
  sum(s.alert_count) as alert_count,
  count(ea.id) as emergency_count
from sessions s
left join emergency_alerts ea on ea.session_id = s.session_id
where s.start_time >= now() - interval '24 hours'
group by s.driver_id
order by emergency_count desc, max_fatigue desc, alert_count desc;
```

## Recomendacion de Backend Administrativo

Aunque la Raspberry ya escribe a Supabase, conviene tener un backend administrativo para:

- Suscribirse a `fleet/+/telemetry`.
- Validar payloads.
- Insertar `telemetry_raw` si se quiere redundancia.
- Generar agregados avanzados.
- Emitir notificaciones de correo/WhatsApp/SMS.
- Controlar permisos y no exponer credenciales MQTT sensibles en frontend.

Responsabilidades minimas:

1. Conectar a EMQX.
2. Suscribirse al topic.
3. Guardar cada mensaje crudo.
4. Crear o actualizar sesion.
5. Crear eventos/emergencias si el payload lo indica.
6. Calcular estado online/offline.
7. Exponer endpoints o usar Supabase Realtime para el panel.

## Variables de Entorno del Backend Administrativo

```env
# MQTT EMQX
EMQX_HOST=hbc0fc94.ala.us-east-1.emqxsl.com
EMQX_PORT=8084
EMQX_USERNAME=<usuario>
EMQX_PASSWORD=<password>
EMQX_TLS=true
MQTT_TRANSPORT=websockets
MQTT_WS_PATH=/mqtt
MQTT_SUBSCRIBE_TOPIC=fleet/+/telemetry
MQTT_QOS=1
MQTT_CLIENT_ID=somnoalert-admin-backend-01

# Supabase
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service-role-key>
SUPABASE_ANON_KEY=<anon-key-solo-frontend>
SUPABASE_SCHEMA=public
```

## Seguridad

- No exponer `SUPABASE_SERVICE_ROLE_KEY` en el frontend.
- No exponer credenciales MQTT administrativas en un frontend publico.
- El frontend debe usar `SUPABASE_ANON_KEY` y politicas RLS.
- El backend debe usar `SUPABASE_SERVICE_ROLE_KEY`.
- Rotar credenciales si fueron compartidas en codigo o documentos publicos.

## Checklist de Implementacion Web

- [ ] Ejecutar `supabase_setup.sql` en Supabase.
- [ ] Confirmar que existen las tablas principales.
- [ ] Configurar EMQX WebSocket/TLS.
- [ ] Probar MQTT con `python test_mqtt_connection.py`.
- [ ] Confirmar que llegan filas a `telemetry_raw`.
- [ ] Crear vista de vehiculos online.
- [ ] Crear vista de alertas y emergencias.
- [ ] Crear vista de sesiones.
- [ ] Crear analitica semanal por conductor.
- [ ] Crear notificaciones para emergencias.
- [ ] Agregar filtros por fecha, vehiculo, conductor y nivel.

## Estado Actual del Proyecto

El proyecto ya cuenta con:

- Publicacion MQTT con soporte WebSockets.
- Sincronizacion Supabase con cola SQLite.
- Persistencia de score ante reinicio.
- Persistencia local de eventos para reglas.
- Menor sensibilidad en deteccion.
- Sonido con retardo de 2 segundos para alertas normales.
- Esquema Supabase en `supabase_setup.sql`.

El siguiente paso recomendado es construir o ajustar el backend administrativo para consumir MQTT y/o leer Supabase, y luego crear dashboards sobre las consultas descritas en este documento.
