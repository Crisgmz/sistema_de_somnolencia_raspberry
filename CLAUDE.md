# Drowsiness Detection System - Raspberry Pi

## Architecture

Real-time drowsiness detection running on Raspberry Pi 4 with camera, GPIO buzzer, MQTT telemetry, and Supabase persistence.

### Pipeline (per frame)
1. **Capture** - Picamera2 (preferred) or OpenCV fallback, 960x720 -> 480x360 for MediaPipe
2. **Detection** - MediaPipe FaceMesh (main thread) + Hands (worker thread, every 4 frames)
3. **Parameters** - 6 extractors in `parametros/`: ojos, boca, cabeza, facial, manos, contexto
4. **Calibration** - First 5 minutes: exponential running average for EAR/MAR/pitch baselines
5. **Scoring** - `DynamicFatigueScore` (0-100) maps to levels 0-4 (NORMAL..EMERGENCIA)
6. **Rules** - `RuleEngine` thread evaluates 5/30/60-min windows, can force minimum alert level
7. **Emergency** - Independent medical pipeline (stroke, seizure, loss of consciousness)
8. **Dispatch** - `AlertDispatcher` routes to buzzer (GPIO 17) + MQTT + Supabase queue
9. **Display** - OpenCV window with parameter panel, status overlay, exit button

### Key directories
- `parametros/` - One class per metric group, each returns dict of param outputs
- `engine/` - Fatigue score, rule engine, emergency detector
- `output/` - Buzzer (GPIO), MQTT publisher, alert dispatcher
- `storage/` - SQLite queue -> Supabase sync thread
- `core/` - Config, calibration, event store, alert memory, common types

### Threading model
- **Main thread**: camera capture, face mesh, parameter extraction, scoring, display
- **HandsWorker**: MediaPipe Hands inference (daemon thread)
- **MqttPublisher**: paho-mqtt client with dynamic publish interval by level
- **SupabaseSync**: SQLite queue flush to Supabase REST API every 15s
- **RuleEngine**: Window-based rule evaluation every 1s
- **Buzzer**: GPIO PWM pattern worker (daemon thread)

### MQTT
- Broker: EMQX Cloud (TLS on port 8883)
- Topic: `fleet/{vehicle_id}/telemetry`
- Publish interval: 10s (normal) down to 1s (emergency)
- Emergency messages published immediately
- QoS configurable (default 1)

### Supabase tables
- `sessions` - Upserted every 15s + final on shutdown
- `telemetry_raw` - Every 2s or immediate on emergency/critical
- `events` - Parameter event transitions
- `emergency_alerts` - Emergency flag transitions
- `metrics_summary` - Per-minute aggregates

### Alert levels
- 0 NORMAL (score <20), 1 FATIGA (20-39), 2 SOMNOLENCIA (40-59), 3 CRITICO (60-79), 4 EMERGENCIA (>=80)

## Running
```bash
# On Raspberry Pi
python main.py

# Environment config in .env (EMQX, Supabase, camera index)
# Display can be disabled: SOMNO_DISPLAY_ENABLED=0
```

## Conventions
- All code and comments in Spanish where practical
- Parameter outputs use `build_param_output()` from `core/common_types.py`
- Each param output dict has: paramid, value, normalized, eventflag, fatiguescoredelta, timestamp
- Calibration-gated: most events only fire after `calibration.calibrated == True`
- No database schema changes without explicit approval
