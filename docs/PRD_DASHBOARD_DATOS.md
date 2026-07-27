# PRD — Contrato de Datos para el Dashboard de Somnolencia

**Versión:** 1.0 · **Fecha:** 2026-07-22 · **Fuente de verdad:** código en `main.py`, `output/mqttpublisher.py`, `output/alertdispatcher.py`, `storage/supabasesync.py`

Este documento define **exactamente qué datos envía el sistema de la Raspberry Pi**, por qué canal, con qué frecuencia y con qué estructura, para que el dashboard consuma datos reales y haga cálculos precisos.

> ⚠️ Las plantillas antiguas (`PLANTILLA_MQTT_SUPABASE_DB.txt`) están desactualizadas respecto al código: el código envía columnas `ts` (no `timestamp`), `session_id` como **texto** (`ses_<12hex>`, no uuid FK), y campos extra (`param_id`, `payload` jsonb, `fatigue_level`, `fatigue_label`, `vehicle_id`/`driver_id` en telemetría). Este PRD refleja lo que el código envía hoy.

---

## 1. Objetivo

Construir un dashboard con dos vistas de datos:

| Vista | Canal | Uso |
|---|---|---|
| **Tiempo real** | MQTT (EMQX Cloud, TLS 8883) | Estado en vivo del conductor, alertas, emergencias |
| **Histórico / analítica** | Supabase (Postgres) | KPIs, tendencias, reportes por sesión/conductor/vehículo |

Regla de oro: **los cálculos precisos se hacen sobre Supabase**, no sobre MQTT. MQTT está muestreado, deduplicado y con histéresis (ver §6), por lo que sirve para "estado ahora", no para agregaciones exactas.

---

## 2. Canal MQTT (tiempo real)

### 2.1 Conexión
- Broker: EMQX Cloud, TLS puerto 8883 (o WebSocket si `MQTT_TRANSPORT=wss`), MQTT v3.1.1, QoS 1 (configurable `MQTT_QOS`).
- Topic principal: `MQTT_TOPIC` (default `test/connection`; soporta plantilla `{vehicle_id}`).
- Topic supervisor: `MQTT_SUPERVISOR_TOPIC`, o por defecto `<topic principal>/supervisor`.

### 2.2 Frecuencia de publicación (dinámica por nivel)

| Nivel | Intervalo telemetría |
|---|---|
| 0 NORMAL | 10 s |
| 1 FATIGA | 5 s |
| 2 SOMNOLENCIA | 5 s (+ publicación inmediata al cambiar nivel/razones) |
| 3 CRITICO | 2 s (+ inmediata) |
| 4 EMERGENCIA | 1 s (+ inmediata) |

Además:
- **Emergencias y nivel ≥ 2 se publican inmediatamente** (sin esperar el intervalo).
- Dedupe: un mismo `(nivel, razones)` no se re-publica antes de 2 s.
- Entre intervalos solo se conserva **el último** payload (los intermedios se descartan): MQTT es una *muestra*, no la serie completa.

### 2.3 Payload de telemetría (topic principal)

Un JSON por mensaje. Estructura completa:

```json
{
  "v": "vehicle_001",
  "d": "driver_001",
  "ts": 1753208400,
  "session_id": "ses_a1b2c3d4e5f6",
  "score": {
    "fatigue_score": 46,
    "level": 2,
    "label": "SOMNOLENCIA",
    "reasons": ["PERCLOS", "EYE_CLOSED_MS"],
    "max_fatigue": 61,
    "alert_count": 7
  },
  "alerts": { "active": true, "level": 2, "reasons": ["PERCLOS", "EYE_CLOSED_MS"] },
  "drowsiness": {
    "committed_level": 2,
    "raw_level": 2,
    "corroborated": true,
    "active_families": ["ocular"],
    "fatigue_score": 46,
    "face_quality_ok": true,
    "landmark_stability": 0.82,
    "active_events": ["PERCLOS"],
    "pose": {
      "yaw_delta": 3.2, "pitch_delta": -1.4, "roll_delta": 0.8,
      "ocular_reliable": true, "mouth_reliable": true,
      "face_illumination": 0.412, "light_ok": true
    },
    "raw": {
      "ear": 0.19, "mar": 0.31, "blink_tc_ms": 420,
      "eye_closed_ms": 0, "perclos": 0.28
    },
    "distraction": { "off_road": false, "sustained": false, "duration_s": 0.0 }
  },
  "emergency": {
    "active": false, "type": null,
    "emergencyflag": false, "emergencytype": null,
    "reasons": [], "fixedbuzzer": false
  },
  "alert_memory": {
    "active_level": 2, "active_reasons": ["PERCLOS"],
    "active_emergency_type": null,
    "active_duration_s": 12.4, "last_transition_s_ago": 12.4,
    "transition_count": 5, "escalation_count": 2,
    "peaks": {}, "emergency_counts": {}, "top_reasons": [], "history_size": 40
  },
  "sys": {
    "fps": 14.2, "status": "online", "calibrated": true,
    "mqtt": { "connected": true, "published_count": 120, "delivered_count": 119, "dropped_count": 0, "queue_size": 0, "level": 2, "last_error": "" },
    "supabase": { "enabled": true, "queued": 500, "flushed": 495, "failed": 0, "pending": 5, "last_error": "", "last_flush_ts": 1753208395.1 }
  }
}
```

**Diccionario de campos clave:**

| Campo | Tipo | Significado |
|---|---|---|
| `v` / `d` | string | vehicle_id / driver_id (de `.env`) |
| `ts` | int | Epoch **UTC en segundos** |
| `session_id` | string | `ses_` + 12 hex, único por arranque del sistema |
| `score.fatigue_score` | int 0–100 | Score dinámico de fatiga |
| `score.level` | int 0–4 | Nivel comprometido (post-corroboración y reglas) |
| `score.label` | string | NORMAL / FATIGA / SOMNOLENCIA / CRITICO / EMERGENCIA |
| `score.reasons` | string[] | Param IDs que dispararon el nivel |
| `score.max_fatigue` | int | Máximo score visto en la sesión |
| `score.alert_count` | int | Nº de eventos nuevos acumulados en la sesión |
| `drowsiness.committed_level` | int | **Usar este** para mostrar nivel (ya corroborado) |
| `drowsiness.raw_level` | int | Nivel antes de corroboración (solo auditoría) |
| `drowsiness.corroborated` | bool | Si el nivel fue confirmado por ≥2 familias de señal |
| `drowsiness.face_quality_ok` | bool | Calidad de detección facial (filtrar métricas si `false`) |
| `drowsiness.raw.*` | float | Valores crudos EAR/MAR/PERCLOS/etc. del frame |
| `emergency.active` / `type` | bool / string | Emergencia médica activa y tipo (`STROKE_PATTERN`, `CONVULSIVE_PATTERN`, etc.) |
| `alert_memory.*` | — | Memoria de transiciones/escaladas de la sesión |
| `sys.*` | — | Salud del dispositivo (FPS, conexión, colas) |

### 2.4 Payload del topic supervisor

Se publica solo cuando **nivel ≥ 3 o emergencia**, con cooldown de 30 s por firma `(nivel, emergencia)`:

```json
{
  "vehicle_id": "vehicle_001",
  "driver_id": "driver_001",
  "ts": 1753208400,
  "session_id": "ses_a1b2c3d4e5f6",
  "level": 3,
  "emergency": false,
  "emergency_type": null,
  "reasons": ["PERCLOS", "EYE_CLOSED_MS"]
}
```

Uso en dashboard: feed de notificaciones críticas / campana de alertas.

---

## 3. Supabase (histórico — fuente para cálculos)

Todas las escrituras pasan por una **cola SQLite local** que se vacía cada 15 s (lotes de 200; los marcados `immediate` fuerzan flush). Implicaciones:

- Los datos pueden llegar con **retraso** (segundos a minutos si no hay red; tras reinicio se drena la cola pendiente).
- **Ordenar siempre por la columna `ts` del registro**, nunca por orden de inserción/`created_at`.
- Todos los `ts` son ISO 8601 **UTC** (`_iso_ts`), p. ej. `2026-07-22T18:00:00+00:00`.

### 3.1 `sessions` — 1 fila por sesión (upsert cada 15 s, final al apagar)

| Columna | Tipo | Notas |
|---|---|---|
| `session_id` | text **PK/único** | `ses_<12hex>` — clave de conflicto del upsert |
| `vehicle_id`, `driver_id` | text | |
| `start_time` | timestamptz | |
| `end_time` | timestamptz \| null | **null = sesión activa** (o cortada sin cierre limpio) |
| `max_fatigue` | int | Score máximo alcanzado |
| `alert_count` | int | Eventos acumulados |

### 3.2 `telemetry_raw` — cada 2 s, o inmediato si emergencia/nivel ≥ 3

| Columna | Tipo | Notas |
|---|---|---|
| `session_id` | text | |
| `ts` | timestamptz | |
| `vehicle_id`, `driver_id` | text | |
| `payload` | jsonb | **El telemetry MQTT completo de §2.3** |

Esta es la tabla más rica: cualquier métrica que no tenga columna propia se extrae del jsonb, p. ej.:

```sql
select ts,
       (payload->'score'->>'fatigue_score')::int  as fatigue_score,
       (payload->'drowsiness'->>'committed_level')::int as level,
       (payload->'drowsiness'->'raw'->>'perclos')::float as perclos,
       (payload->'drowsiness'->'raw'->>'ear')::float as ear
from telemetry_raw
where session_id = :sid
order by ts;
```

### 3.3 `events` — 1 fila por **transición a activo** de cada parámetro

Solo se inserta cuando un parámetro pasa de inactivo → activo (no se repite mientras siga activo).

| Columna | Tipo | Notas |
|---|---|---|
| `session_id` | text | |
| `ts` | timestamptz | Inicio del evento |
| `event_type` | text | = param_id (p. ej. `PERCLOS`, `YAWN_FREQ`) |
| `severity_level` | int | Nivel de alerta en ese momento |
| `param_id` | text | |
| `param_value` | float | Valor crudo al disparar |
| `duration_ms` | int | **Siempre 0 hoy** — no usar para duraciones (ver §7) |
| `fatigue_score` | int | Score en ese momento |
| `payload` | jsonb | `{telemetry_ts, normalized, fatiguescoredelta, alert_memory}` |

### 3.4 `emergency_alerts` — inmediato, 1 fila por transición de tipo de emergencia

| Columna | Tipo | Notas |
|---|---|---|
| `session_id` | text | |
| `ts` | timestamptz | |
| `emergency_type` | text | `STROKE_PATTERN`, `CONVULSIVE_PATTERN`, `LOSS_OF_CONSCIOUSNESS`, `UNKNOWN`… |
| `trigger_params` | jsonb | `{reasons, fatigue_score, level, alert_memory}` |
| `duration_seconds` | float | **Siempre 0 hoy** |
| `resolved_at`, `resolution_type` | null | Reservados — el dispositivo no los llena; los llenaría el panel |
| `payload` | jsonb | Telemetría completa del momento |

### 3.5 `metrics_summary` — 1 fila por minuto (agregado en dispositivo)

Promedios del minuto sobre muestras por-frame:

| Columna | Tipo | Notas |
|---|---|---|
| `session_id` | text | |
| `ts` | timestamptz | Fin del minuto |
| `avg_ear`, `avg_mar`, `avg_pitch` | float | Promedios del minuto |
| `perclos` | float | Promedio del PERCLOS (ya es % de párpado cerrado en ventana) |
| `blink_freq` | float | Parpadeos/min promedio |
| `fatigue_score` | int | **Promedio** del minuto (no el máximo) |
| `fatigue_level` | int | Promedio redondeado del nivel |
| `fatigue_label` | text | Label del último frame del minuto |
| `illumination` | **text** | ⚠️ guardado como string (`"0.412"`) — castear a float |
| `time_on_task` | int | Minutos de conducción continua |
| `monotony_index` | int | Índice de monotonía |
| `payload` | jsonb | `{samples: N, alert_memory}` — `samples` = nº de frames promediados |

---

## 4. Diccionario de parámetros (26 param IDs)

Cada parámetro genera `{paramid, value, normalized (0–1), eventflag, fatiguescoredelta}`. Los eventos solo disparan **después de calibración** (~primeros 5 min, salvo excepciones inmediatas como `EYE_CLOSED_MS`).

| Familia | Param ID | Valor (`value`) | Evento cuando | Δ score |
|---|---|---|---|---|
| Ojos | `EAR` | Eye Aspect Ratio | ear < umbral calibrado | +2 |
| Ojos | `EYE_CLOSED_MS` | ms ojos cerrados | ≥ umbral microsueño | +8 / +12 (≥1.5 s) |
| Ojos | `PERCLOS` | % párpado cerrado (ventana) | ≥ onset | +10 / +14 (severo) |
| Ojos | `BLINK_TC` | duración cierre parpadeo (ms) | lento vs baseline | +6 |
| Ojos | `BLINK_FB` | parpadeos/min | < 4 o > 32 | +5 |
| Ojos | `IBI` | intervalo entre parpadeos (s) | ≥ 8 s | +4 |
| Ojos | `BLINK_AMPLITUDE` | amplitud parpadeo | anómala | +3 |
| Ojos | `REOPEN_SPEED` | velocidad reapertura | lenta | +5 |
| Ojos | `FIXATION` | fijación de mirada (s) | ≥ 8 s | +4 |
| Boca | `MAR` | Mouth Aspect Ratio | bostezo | +4 |
| Boca | `YAWN_FREQ` | bostezos acumulados | ≥ 4 | +5 |
| Boca | `YAWN_DUR` | duración último bostezo (s) | ≥ 2 s | +4 |
| Cabeza | `PITCH` | pitch (°) | \|Δ\| ≥ 24° | +5 |
| Cabeza | `ROLL` | roll (°) | \|Δ\| ≥ 32° | +4 |
| Cabeza | `YAW` | yaw (°) | \|Δ\| ≥ 40° | +2 |
| Cabeza | `HEAD_DROP_VELOCITY` | °/s de caída | ≥ 25 | +6 |
| Cabeza | `HEAD_RECOVERY` | s en recuperar postura | lento | +5 |
| Cabeza | `HEAD_MICRO_OSC` | oscilación (índice) | ≥ 0.55 | 0 (señal médica) |
| Facial | `LANDMARK_STABILITY` | estabilidad 0–1 | ≤ 0.35 | +4 |
| Facial | `MUSCLE_TONE` | caída de tono | ≥ 0.2 | +3 |
| Facial | `FACIAL_ASYMMETRY` | asimetría | ≥ 2× baseline | 0 (→ emergencia STROKE) |
| Manos | `EYE_RUB` | frames frotando ojos | detectado | +4 |
| Manos | `FACE_TOUCH_FREQ` | toques cara/min | ≥ 7 | +2 |
| Manos | `FACE_TOUCH_DUR` | duración toque (ms) | ≥ 2000 | +2 |
| Contexto | `TIME_ON_TASK` | min conduciendo | ≥ 120 | +2 |
| Contexto | `CIRCADIAN` | multiplicador circadiano | > 1.0 | 0 |
| Contexto | `MONOTONY` | índice monotonía | ≥ 5 | +2 |
| Contexto | `ILLUMINATION` | brillo facial 0–1 | < 0.15 | +1 |

**Dinámica del score:** el delta se aplica **una sola vez por transición** a activo (no por frame). Sin eventos frescos, tras un periodo de gracia el score decae linealmente (pts/seg). Nivel: 0 (<20), 1 (20–39), 2 (40–59), 3 (60–79), 4 (≥80). El `RuleEngine` (ventanas 5/30/60 min) puede **forzar un nivel mínimo**.

---

## 5. Cadencias — resumen

| Dato | Canal | Frecuencia |
|---|---|---|
| Telemetría en vivo | MQTT | 10 s → 1 s según nivel; inmediata si nivel ≥ 2 o emergencia |
| Alerta supervisor | MQTT `/supervisor` | Al entrar a nivel ≥ 3 / emergencia, cooldown 30 s |
| `telemetry_raw` | Supabase | Cada 2 s; inmediata si nivel ≥ 3 o emergencia |
| `sessions` | Supabase | Upsert cada 15 s + final al apagar |
| `events` | Supabase | En cada transición de parámetro a activo |
| `emergency_alerts` | Supabase | Inmediata en transición de tipo de emergencia |
| `metrics_summary` | Supabase | 1/minuto |
| Flush de cola → Supabase | — | Cada 15 s (lotes de 200) |

---

## 6. Qué canal usar para cada cálculo (precisión)

| Necesidad del dashboard | Fuente correcta | Por qué |
|---|---|---|
| Nivel/score "ahora", semáforo en vivo | MQTT `drowsiness.committed_level` | Tiempo real |
| Notificaciones críticas | MQTT topic supervisor | Ya filtrado + cooldown |
| Serie temporal de score / EAR / PERCLOS | `telemetry_raw` (jsonb) | Cadencia fija 2 s, sin dedupe |
| Tendencias por minuto / gráficas largas | `metrics_summary` | Ya agregado, barato de consultar |
| Conteo y tipos de eventos, ranking de causas | `events` | 1 fila por transición real |
| Historial de emergencias | `emergency_alerts` | Inmediato y completo |
| KPIs por sesión (máximo, nº alertas, duración) | `sessions` | Upsert autoritativo |
| **Nunca** para agregaciones | MQTT | Muestreado + dedupe + histéresis de 3 s |

**Recetas de cálculo precisas:**

- **% de tiempo por nivel** (por sesión): sobre `telemetry_raw`, tomar `(payload->'drowsiness'->>'committed_level')` y ponderar cada fila por el gap hasta la siguiente (`lead(ts) over (order by ts) - ts`), con tope de ~5 s para no inflar huecos por desconexión.
- **Alertas/hora**: `count(*)` en `events` con `severity_level >= 2` dividido por horas de sesión (`coalesce(end_time, max ts) - start_time`).
- **Duración de un episodio**: `duration_ms`/`duration_seconds` vienen en 0 — calcular como diferencia entre el `ts` del evento y el primer `telemetry_raw` posterior donde el parámetro deja de estar en `drowsiness.active_events`.
- **PERCLOS promedio confiable**: filtrar frames con `face_quality_ok = false` y `pose.ocular_reliable = false` antes de promediar (ambos están en el jsonb de `telemetry_raw`).
- **Sesión activa**: `end_time is null` **y** último `telemetry_raw` < 60 s de antigüedad (si el equipo se apaga sin red, `end_time` queda null hasta el próximo drenado).
- **Excluir calibración**: los primeros ~5 min de cada sesión tienen `sys.calibrated = false`; excluirlos de KPIs de fatiga (los eventos están gateados, pero el score/valores crudos existen).

---

## 7. Limitaciones conocidas (para no calcular mal)

1. `events.duration_ms` y `emergency_alerts.duration_seconds` **siempre llegan en 0** — el dispositivo no los actualiza al cerrar el episodio.
2. `emergency_alerts.resolved_at` / `resolution_type` son responsabilidad del **panel**, no del dispositivo.
3. `metrics_summary.illumination` llega como **texto**; castear.
4. `metrics_summary.fatigue_score` es el **promedio** del minuto — para picos usar `telemetry_raw` o `sessions.max_fatigue`.
5. Datos pueden llegar tarde y en ráfaga (cola offline) — dashboards "en vivo" sobre Supabase deben re-consultar ventanas pasadas, no solo la última fila.
6. Todos los timestamps son **UTC** — convertir a zona local solo en presentación.
7. Con `vehicle_id`/`driver_id` por defecto (`vehicle_unknown`/`driver_unknown`) los agrupados por vehículo/conductor no discriminan; configurar `.env` en cada equipo.

---

## 8. Variables de entorno relevantes del emisor

```
MQTT: EMQX_HOST, EMQX_PORT (8883), MQTT_TOPIC, MQTT_SUPERVISOR_TOPIC,
      MQTT_TRANSPORT (tcp|wss), MQTT_QOS (1), EMQX_USERNAME, EMQX_PASSWORD
IDs:  VEHICLE_ID, DRIVER_ID
DB:   SUPABASE_URL, SUPABASE_KEY, SQLITE_QUEUE_PATH
```
