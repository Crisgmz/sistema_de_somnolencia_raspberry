# Drowsiness Detection System - Raspberry Pi

## Architecture

Real-time drowsiness detection running on Raspberry Pi 4 with camera, GPIO buzzer, MQTT telemetry, and Supabase persistence. Design rationale in `docs/PLAN_TRABAJO_PRECISION.md`.

### Pipeline (per frame)
1. **Capture** - Picamera2 (preferred) or OpenCV fallback, 640x480, aspect preserved for MediaPipe
2. **Detection** - MediaPipe FaceMesh with `refine_landmarks=True` always (key for glasses robustness)
3. **Parameters** - 5 extractors in `parametros/`: ojos, boca, cabeza, facial, contexto
4. **Calibration** - First 90s with stable face: robust MEDIAN baselines for EAR/MAR/asymmetry, circular mean for pose neutral. Persisted per driver
5. **Scoring** - `DynamicFatigueScore` (0-100) maps to levels 0-4 (NORMAL..EMERGENCIA). Always starts at 0 per session - NEVER restore old scores (removed by design: it caused phantom alerts at startup)
6. **Rules** - `RuleEngine` thread evaluates 5/30-min windows over event transitions, can force minimum alert level
7. **Emergency** - Independent pipeline with ONLY two real triggers: LOSS_OF_CONSCIOUSNESS (eyes closed >=2s) and PROLONGED_HEAD_DOWN (>=6s). Speculative medical patterns (stroke/lateral collapse/dissociation/face-out) are telemetry-only `suspicions` - they never sound or force a level
8. **Dispatch** - `AlertDispatcher` routes to buzzer (GPIO 17) + MQTT + Supabase queue
9. **Display** - OpenCV window with parameter panel, status overlay, exit button

### Scoring signals (IMPORTANT)
Only 4 params generate events with fatigue-score weight:
- `EYE_CLOSED_MS` - sustained eye closure >= 500ms (primary trigger, LearnOpenCV-style)
- `PERCLOS` - P80 fraction over 60s window
- `MAR` - sustained yawn (>= 500ms open)
- `PITCH` - sustained head nod vs calibrated neutral

Everything else (BLINK_TC/FB, IBI, amplitude, reopen speed, fixation, ROLL/YAW, facial, context) is telemetry-only: `eventflag=False, fatiguescoredelta=0`. Do NOT re-add weight to noisy params - that was the main false-positive source. The hands pipeline (MediaPipe Hands + parametros/manos) and the head micro-oscillation FFT were removed entirely: CPU cost + noise, no reliable signal.

The eye-closure threshold is STABLE: `close_thr = SOMNO_EAR_CLOSE_FRAC (0.70) x ear_baseline`, frozen after calibration. Never reintroduce per-frame adaptive thresholds (open_ref envelope) or glasses/night threshold scales.

### Clock discipline (IMPORTANT)
- `time.monotonic()` for ALL internal logic: windows, durations, cadences, calibration, score recovery, rule engine, dispatcher/MQTT intervals. The Pi has no RTC; NTP jumps the wall clock mid-session (typical on LTE) and used to corrupt every time window.
- `time.time()` ONLY at the edges: ISO timestamps for Supabase/MQTT payloads, SQLite `updated_at` stamps.
- In `main.run()`: `ts` = monotonic, `wall_ts` = wall. Keep it that way.

### Key directories
- `parametros/` - One class per metric group, each returns dict of param outputs
- `engine/` - Fatigue score, rule engine, level stabilizer (corroboration), emergency detector
- `output/` - Buzzer (GPIO), MQTT publisher, alert dispatcher
- `storage/` - SQLite queue -> Supabase sync thread, session recorder
- `core/` - Config, calibration, vision helpers (EAR/MAR/pose), event store (RAM), alert memory, common types
- `tools/` - `validate_video.py` (offline harness: run a clip through the real ocular pipeline), `validate_precision.py`, `test_buzzer.py`

### Threading model
- **Main thread**: camera capture, face mesh, parameter extraction, scoring, display
- **MqttPublisher**: paho-mqtt client with dynamic publish interval by level
- **SupabaseSync**: SQLite queue flush to Supabase every 15s, BATCHED (grouped insert/upsert lists, not per-row requests). Queue capped at 20k rows (old telemetry pruned, events/emergencies never)
- **RuleEngine**: Window-based rule evaluation every 1s
- **Buzzer**: GPIO PWM pattern worker (daemon thread)

### MQTT
- Broker: EMQX Cloud (TLS on port 8883)
- Publish interval: 10s (normal) down to 1s (emergency); emergency published immediately
- QoS: telemetry uses `MQTT_QOS_TELEMETRY` (default 0); alerts/emergency use `MQTT_QOS` (default 1)
- max_inflight=10, max_queued=50 (avoid reconnect bursts on LTE)

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
./run.sh          # uses .venv interpreter

# Environment config in .env (EMQX, Supabase, camera index)
# Display can be disabled: SOMNO_DISPLAY_ENABLED=0

# Record video FROM the live pipeline (raw frames, no HUD) for offline validation:
#   SOMNO_RECORD_VIDEO=1 ./run.sh   -> recordings/<session_id>.avi (MJPG, 10 FPS, max 600s)
# Then validate false positives/min and EAR margin:
python tools/validate_video.py recordings/ses_xxxx.avi
```

## Conventions
- All code and comments in Spanish where practical
- Parameter outputs use `build_param_output()` from `core/common_types.py`
- Each param output dict has: paramid, value, normalized, eventflag, fatiguescoredelta, timestamp
- Calibration-gated: most events only fire after `calibration.calibrated == True`
- No database schema changes without explicit approval
