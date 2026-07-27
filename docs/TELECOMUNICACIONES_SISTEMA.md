# SomnoAlert - Arquitectura de Telecomunicaciones

## Resumen ejecutivo

SomnoAlert es un sistema embebido de deteccion de somnolencia en tiempo real que corre sobre una Raspberry Pi 4. Para transmitir datos de fatiga, alertas y emergencias hacia la nube, el sistema usa el protocolo MQTT sobre una conexion cifrada TLS, con un broker EMQX Cloud alojado en AWS. Los datos persisten en Supabase (PostgreSQL gestionado) y se respaldan localmente en SQLite ante perdida de conectividad.

---

## 1. Topologia de la red

```
Raspberry Pi 4
  (camara CSI + buzzer GPIO)
        |
        | MQTT / WebSocket / TLS
        | Puerto 8084
        v
   EMQX Cloud (AWS us-east-1)
   hbc0fc94.ala.us-east-1.emqxsl.com
        |
        | suscripcion MQTT
        v
   Backend administrativo
        |
        | REST / Supabase SDK
        v
   Supabase (PostgreSQL)
        |
        | Supabase Realtime / consultas SQL
        v
   Panel web administrativo
```

---

## 2. Hardware de comunicacion

| Componente | Descripcion |
|---|---|
| Raspberry Pi 4 | Unidad de computo y comunicacion. Conectividad Wi-Fi 802.11ac integrada o Ethernet. |
| Arducam IMX519 | Camara CSI para captura de video. No tiene componente de red propio; todo el trafico lo maneja la Raspberry. |
| Buzzer GPIO | Actuador local (sin red). Recibe senales desde el pin GPIO 17. |
| Red local | La Raspberry se conecta a internet mediante la red del vehiculo/laboratorio (Wi-Fi o LAN). |

---

## 3. Protocolo MQTT

### 3.1 Que es MQTT

MQTT (Message Queuing Telemetry Transport) es un protocolo ligero de mensajeria publicador/suscriptor disenado para dispositivos IoT con ancho de banda limitado. Opera sobre TCP/IP y fue elegido para este sistema por:

- Muy bajo overhead de cabeceras (2 bytes minimo).
- Modelo pub/sub desacoplado: la Raspberry publica sin conocer a los suscriptores.
- QoS configurable: garantia de entrega incluso en redes inestables.
- Soporte nativo de reconexion automatica.

### 3.2 Configuracion del cliente MQTT (Raspberry)

| Parametro | Valor |
|---|---|
| Broker host | `hbc0fc94.ala.us-east-1.emqxsl.com` |
| Puerto TLS (TCP) | `8883` |
| Puerto TLS (WebSocket) | `8084` |
| Transporte activo | WebSockets (`MQTT_TRANSPORT=websockets`) |
| Path WebSocket | `/mqtt` |
| Client ID | `raspi-somnoalert-001` |
| QoS | 1 (al menos una entrega) |
| Keepalive | 60 segundos |
| Reconexion automatica | 1 s minimo, 30 s maximo |
| Autenticacion | Usuario/contrasena |
| Cifrado | TLS (sin certificado de cliente; el broker verifica el servidor) |

### 3.3 Por que WebSockets en lugar de TCP puro

El transporte WebSocket permite que el trafico MQTT atraviese proxies, firewalls y redes corporativas que bloquean puertos TCP no estandar. Puerto 8084 es HTTPS alternativo y pasa la mayoria de restricciones de red en entornos vehiculares o empresariales.

### 3.4 Calidad de servicio (QoS)

| Nivel | Garantia |
|---|---|
| QoS 0 | Entrega a lo sumo una vez (sin confirmacion). |
| **QoS 1** | **Entrega al menos una vez (confirmacion del broker). Usado en SomnoAlert.** |
| QoS 2 | Exactamente una vez (doble handshake). Mayor latencia. |

QoS 1 garantiza que ningun mensaje de telemetria se pierda sin ser confirmado, tolerando retrasmisiones en redes inestables.

---

## 4. Topics MQTT

### 4.1 Esquema de topics

```
fleet/{vehicle_id}/telemetry          <- telemetria principal (cada 1-10 s segun nivel)
fleet/{vehicle_id}/telemetry/supervisor  <- mensajes de supervision/control
```

### 4.2 Topic de prueba (entorno de desarrollo)

```
test/connection
test/connection/supervisor
```

### 4.3 Suscripcion para flotas

El backend administrativo usa comodines MQTT:

```
fleet/+/telemetry          <- todos los vehiculos
fleet/+/telemetry/supervisor
```

---

## 5. Frecuencia de publicacion por nivel de alerta

La Raspberry ajusta dinamicamente el intervalo de publicacion segun el nivel de fatiga detectado. A mayor nivel, mayor frecuencia para que el panel web reaccione mas rapido.

| Nivel | Etiqueta | Score | Intervalo MQTT |
|---|---|---|---|
| 0 | NORMAL | < 20 | 10 segundos |
| 1 | FATIGA | 20 - 39 | 5 segundos |
| 2 | SOMNOLENCIA | 40 - 59 | 5 segundos |
| 3 | CRITICO | 60 - 79 | 2 segundos |
| 4 | EMERGENCIA | >= 80 | **1 segundo / inmediato** |

Las emergencias medicas (derrame, convulsion, perdida de consciencia) se publican **inmediatamente** sin esperar el intervalo.

---

## 6. Payload MQTT (JSON)

Cada mensaje publicado tiene la siguiente estructura:

```json
{
  "v": "truck_042",
  "d": "driver_007",
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
    "active": false
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

---

## 7. Cifrado y seguridad

| Capa | Mecanismo |
|---|---|
| Transporte | TLS 1.2/1.3 entre Raspberry y EMQX Cloud |
| Autenticacion | Usuario y contrasena MQTT |
| Broker | EMQX Cloud gestionado (AWS us-east-1), sin exposicion de servidor propio |
| Base de datos | Supabase con Row Level Security (RLS); frontend usa Anon Key, backend usa Service Role Key |
| Secretos | Credenciales en archivo `.env` local, nunca embebidas en codigo fuente |

---

## 8. Persistencia de datos

### 8.1 Cola local SQLite (resiliente)

La Raspberry mantiene una base de datos SQLite local (`somnolencia_queue.db`) con las siguientes tablas:

| Tabla | Proposito |
|---|---|
| `score_state` | Estado del score de fatiga. Se restaura al reiniciar, evitando caidas falsas en el panel. |
| `event_history` | Historial de eventos para que el motor de reglas evalue ventanas de tiempo sin depender de RAM. |
| `telemetry_queue` | Cola de mensajes pendientes de enviar a Supabase cuando no hay conexion a internet. |

Si la Raspberry pierde internet, los datos se acumulan en SQLite y se sincronizan automaticamente cuando se restablece la conexion.

### 8.2 Supabase (PostgreSQL en la nube)

| Tabla | Contenido |
|---|---|
| `sessions` | Una fila por sesion de conduccion. Se actualiza cada 15 s y al cerrar sesion. |
| `telemetry_raw` | Cada mensaje MQTT completo en JSON. Fuente de verdad para el panel en tiempo real. |
| `metrics_summary` | Resumen por minuto: EAR, MAR, PERCLOS, score, iluminacion, monotonia. |
| `events` | Eventos de parametros: ojos, boca, cabeza, manos, contexto. |
| `emergency_alerts` | Emergencias medicas o criticas con timestamp de inicio y resolucion. |

### 8.3 Flujo de sincronizacion

```
Raspberry (deteccion)
    |
    | -> cola SQLite local (inmediato)
    |
    | -> MQTT -> EMQX -> backend (tiempo real)
    |
    | -> SupabaseSync thread (cada 15 s o inmediato en emergencia)
    |        |
    |        v
    |    Supabase REST API
    |        |
    |        v
    |    Panel web (lectura Supabase Realtime / SQL)
```

---

## 9. Hilo de publicacion MQTT (arquitectura de software)

El publicador MQTT corre en un hilo independiente (`MqttPublisher`, daemon thread) para no bloquear el pipeline principal de deteccion:

- El hilo principal encola mensajes de telemetria via `enqueue()`.
- El hilo MQTT los publica al broker segun el intervalo del nivel actual.
- Emergencias se enqueuen con `kind=immediate` y se publican sin esperar el intervalo.
- Cola maxima de 1000 mensajes; si se llena, se descartan los mas antiguos y se cuenta en `dropped_count`.
- Reconexion automatica con backoff exponencial (1 s → 30 s).

---

## 10. Prueba de conectividad

Para verificar que la conexion MQTT funciona correctamente desde la Raspberry:

```bash
python test_mqtt_connection.py
```

Salida esperada:

```
[OK] MQTT publicado en topic=test/connection transport=websockets
```

---

## 11. Variables de entorno requeridas

```env
# Broker EMQX Cloud
EMQX_HOST=hbc0fc94.ala.us-east-1.emqxsl.com
EMQX_PORT=8084
EMQX_USERNAME=<usuario>
EMQX_PASSWORD=<password>
EMQX_TLS=true

# Transporte y topic
MQTT_TRANSPORT=websockets
MQTT_WS_PATH=/mqtt
MQTT_TOPIC=fleet/{vehicle_id}/telemetry
MQTT_QOS=1
MQTT_CLIENT_ID=raspi-somnoalert-001

# Identidad del vehiculo/conductor
VEHICLE_ID=truck_042
DRIVER_ID=driver_007

# Supabase
SUPABASE_URL=https://<proyecto>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service-role-key>
```

---

## 12. Diagrama de secuencia (mensaje de emergencia)

```
Raspberry Pi          EMQX Cloud          Supabase
     |                    |                   |
     | deteccion emergencia                   |
     |--publish(immediate)-->|                |
     |                    |--broker ACK------>|
     |                    |                   |
     |--SupabaseSync------------------------------> INSERT emergency_alerts
     |                    |                   |
     | (panel web suscrito a Supabase Realtime recibe notificacion en < 1 s)
```

---

## 13. Resumen tecnologico

| Componente | Tecnologia |
|---|---|
| Dispositivo embebido | Raspberry Pi 4 (ARM Cortex-A72, 4 GB RAM) |
| Sistema operativo | Raspberry Pi OS (Debian 12) |
| Lenguaje | Python 3.12 |
| Cliente MQTT | paho-mqtt 2.x |
| Broker en la nube | EMQX Cloud (AWS us-east-1) |
| Transporte | WebSocket sobre TLS (puerto 8084) |
| Base de datos local | SQLite 3 |
| Base de datos en la nube | Supabase / PostgreSQL |
| SDK Supabase | supabase-py |
| Deteccion visual | MediaPipe FaceMesh + OpenCV |
| Captura de video | Picamera2 (Arducam IMX519, CSI) |
| Actuador local | Buzzer PWM en GPIO 17 |
