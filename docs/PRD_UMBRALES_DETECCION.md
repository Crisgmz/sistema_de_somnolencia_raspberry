# PRD — Umbrales de Detección del Sistema de Somnolencia

**Producto:** Sistema de detección de somnolencia — Raspberry Pi
**Versión de referencia:** Configuración de demo estable (2026-07-27)
**Alcance:** Documento de referencia de TODOS los umbrales que gobiernan la detección, el scoring, las emergencias y las alertas. Valores extraídos directamente del código; los marcados **(env)** se ajustan sin recompilar vía `.env`.

---

## 1. Pipeline de decisión (resumen)

```
Cámara 640×480 → MediaPipe FaceMesh (640px) → 4 señales con peso:
  EYE_CLOSED_MS · PERCLOS · MAR · (PITCH*)   → Score 0-100 → Nivel 0-4 → Buzzer/MQTT
                                             → Pipeline emergencia (ojos cerrados)
(*) PITCH suprimido en esta configuración por inestabilidad de pose.
```

**Señales que PUNTÚAN el score hoy:** `EYE_CLOSED_MS`, `PERCLOS`, `MAR`.
**Señales SUPRIMIDAS** (telemetría/panel, delta 0): `PITCH`, `ROLL`, `YAW`, `HEAD_DROP_VELOCITY`, `HEAD_RECOVERY`, `EAR`, `BLINK_*`, `IBI`, `FIXATION`, `LANDMARK_STABILITY`, `MUSCLE_TONE`, `FACIAL_ASYMMETRY`, contexto.

---

## 2. Captura e imagen

| Parámetro | Valor | Env |
|---|---|---|
| Resolución de captura | 640 × 480 | — |
| Lado mayor para MediaPipe | 640 px | `SOMNO_MP_LONG` |
| Calentamiento de cámara (sin puntuar) | 6 s | `SOMNO_WARMUP_S` |
| Rotación de imagen | 0 | `CAMERA_ROTATION` |
| Formato / color | BGR888 → volteo R↔B a BGR | — |

---

## 3. Detección ocular (`parametros/ojos.py`)

| Parámetro | Valor | Env |
|---|---|---|
| Fracción de cierre (close_thr = frac × apertura) | **0.70** | `SOMNO_EAR_CLOSE_FRAC` |
| Histéresis de reapertura (open_thr = close_thr + h) | 0.03 | `SOMNO_EAR_HYSTERESIS` |
| Piso absoluto de close_thr | 0.10 | — |
| **Microsueño** (EYE_CLOSED_MS dispara) | **≥ 500 ms** cerrado | `SOMNO_MICROSLEEP_MS` |
| PERCLOS criterio P80 (cierre ≥80%) | 0.80 | `SOMNO_PERCLOS_CLOSE_FRAC` |
| PERCLOS piso EAR ojo cerrado | 0.08 | `SOMNO_PERCLOS_CLOSED_FLOOR` |
| **PERCLOS onset** (dispara SOMNOLENCIA) | **0.25** (default 0.15) | `SOMNO_PERCLOS_ONSET` |
| PERCLOS severo | 0.30 | `SOMNO_PERCLOS_SEVERE` |
| Ventana PERCLOS | 60 s | — |

> El umbral de cierre es **estable**: `close_thr = 0.70 × apertura de referencia`, derivado de la calibración.

---

## 4. Detección de boca / bostezo (`parametros/boca.py`)

| Parámetro | Valor |
|---|---|
| Umbral MAR (boca abierta) | `max(0.34, mar_baseline × 1.4)` |
| **MAR puntúa solo con apertura sostenida** | **≥ 500 ms** |
| Conteo de bostezo (apertura válida) | ≥ 0.8 s |

---

## 5. Detección de cabeza / pose (`parametros/cabeza.py`)

| Parámetro | Valor | Estado |
|---|---|---|
| PITCH (cabeceo) | \|pitch_delta\| ≥ 24° | **suprimido** (pose inestable) |
| Ventana de velocidad de cabeceo | 0.35 s | — |
| HEAD_DROP_VELOCITY | 25–130 °/s | **suprimido** |
| ROLL / YAW / HEAD_RECOVERY | 32° / 40° / 2.3 s | **suprimidos** |
| Inicio/fin de recuperación de cabeceo | ≥24° / ≤12° | — |

---

## 6. Calidad facial y fiabilidad geométrica (`facial.py`, `main.py`)

| Parámetro | Valor | Env |
|---|---|---|
| Estabilidad de landmarks | `1/(1+35·desplazamiento_norm)` | — |
| **Gate de calidad de rostro** (para puntuar/calibrar) | **≥ 0.60** | `SOMNO_FACE_QUALITY_MIN` |
| Yaw máx. para fiar de ojos | 38° | `SOMNO_YAW_OCULAR_MAX` |
| Pitch máx. para fiar de ojos | 28° | `SOMNO_PITCH_OCULAR_MAX` |
| Roll máx. para fiar de ojos | 35° | `SOMNO_ROLL_OCULAR_MAX` |
| Yaw / Pitch máx. para fiar de boca | 45° / 32° | `SOMNO_YAW_MOUTH_MAX`, `SOMNO_PITCH_MOUTH_MAX` |
| Distracción (mirada fuera de vía) | yaw ≥ 40° | `SOMNO_DISTRACTION_YAW` |

---

## 7. Iluminación (`main.py`, `contexto.py`)

| Condición | Umbral | Comportamiento |
|---|---|---|
| **Mínimo para operar** (light_ok) | **0.12** brillo rostro | `SOMNO_MIN_ILLUMINATION`; por debajo se apaga detección |
| Modo noche automático | < 0.28 | Ajusta umbrales |
| Mínimo práctico confiable | ~0.30–0.40 | (recomendación operativa) |
| Oscuridad total sin IR | — | ❌ No funciona (roadmap: LED IR/HDR) |

---

## 8. Score de fatiga (`engine/fatiguescore.py`)

**Niveles:** 0 NORMAL (<20) · 1 FATIGA (20–39) · 2 SOMNOLENCIA (40–59) · 3 CRÍTICO (60–79) · 4 EMERGENCIA (≥80)

| Parámetro | Valor | Env |
|---|---|---|
| Delta EYE_CLOSED_MS | +8 (+12 si ≥1500 ms) | — |
| Delta PERCLOS | +10 (+14 si ≥ severo) | — |
| Delta MAR | +4 | — |
| Delta PITCH | +5 *(suprimido)* | — |
| **Ganancia de subida** | **× 0.5** | `SOMNO_RISE_GAIN` |
| **Gracia antes de recuperar** | **3 s** (default 300) | `SOMNO_RECOVERY_GRACE_S` |
| **Velocidad de bajada** | **8 pts/s** (default 3) | `SOMNO_RECOVERY_PER_S` |
| Gracia pérdida de rostro | 4 s | `SOMNO_FACE_GRACE_S` |
| Decaimiento sin rostro | 3 pts/s | `SOMNO_FACE_DECAY_PER_S` |
| Override rápido a nivel 2 | eye_closed ≥ 1500 ms | — |
| Restaurar score al arrancar | 0 (arranca limpio) | `SOMNO_RESTORE_SCORE_CAP` |

---

## 9. Emergencias (`engine/emergencydetector.py`)

| Trigger | Umbral | Estado | Env |
|---|---|---|---|
| **LOSS_OF_CONSCIOUSNESS** (ojos cerrados) | **≥ 4000 ms** (default 2000) | ✅ activo | `SOMNO_EYE_CLOSED_EMERGENCY_MS` |
| **PROLONGED_HEAD_DOWN** (cabeza abajo) | pitch ≤ -24° por ≥6 s | ❌ **desactivado** | `SOMNO_HEAD_DOWN_EMERGENCY=0`, `SOMNO_HEAD_DOWN_DEG`, `SOMNO_HEAD_DOWN_S` |
| Sospechas (solo telemetría, no suenan) | ictus asim ≥ thr; colapso roll≥45° & yaw≥30°; etc. | informativo | — |

---

## 10. Estabilizador de nivel / corroboración (`engine/corroboration.py`)

| Parámetro | Valor | Env |
|---|---|---|
| Tiempo de subida por nivel (t_up) | (0, 1.0, 1.5, 2.0, 2.5) s | `SOMNO_TUP_1..4_S` |
| Tiempo de bajada por nivel (t_down) | 1.2 s | `SOMNO_TDOWN_S` |
| Nivel mínimo que exige corroboración | 3 | `SOMNO_CORROB_MIN_LEVEL` |

---

## 11. Motor de reglas de ventana (`engine/ruleengine.py`) — **DESACTIVADO**

Estado: `SOMNO_RULES_ENABLED=0` (el nivel sigue al score actual). Reglas si se reactiva:

| Regla | Condición (ventana) | Fuerza nivel |
|---|---|---|
| Cierre largo | EYE_CLOSED ≥ 2000 ms en 5 min | 4 |
| Fatiga cruzada | PERCLOS ≥4 y EYE_CLOSED ≥3 en 30 min | 3 |
| Racha de cabeceos | PITCH ≥3 en 5 min | 2 |
| Racha de bostezos | MAR ≥3 en 30 min | 1 |

---

## 12. Buzzer y alertas (`output/alertdispatcher.py`, `output/buzzer.py`)

| Parámetro | Valor | Env |
|---|---|---|
| **Frecuencia del tono (piezo pasivo PWM)** | **2700 Hz** | `SOMNO_BUZZER_FREQ` |
| Ciclo de trabajo (volumen) | 50 % | `SOMNO_BUZZER_DUTY` |
| **Retardo antirebote antes de sonar** | **0.1 s** (default 2.0) | `SOMNO_SOUND_DELAY_S` |
| Auto-silencio tras despejado | 30 s | `SOMNO_BUZZER_QUIET_S` |
| Mantener nivel antes de bajar | 3 s | — |
| **Sin rostro → no suena** | — | (regla fija) |
| Patrón buzzer por nivel (on/off s) | N1 0.08/0.92 · N2 0.12/0.48 · N3 0.16/0.24 · N4 0.22/0.08 | — |
| Dedup MQTT | 2 s | — |
| Cooldown notificación supervisor | 30 s | — |

---

## 13. Calibración (`core/calibration.py`)

| Parámetro | Rango válido | Default |
|---|---|---|
| Duración de calibración | — | **90 s** (`CALIBRATION_SECONDS`) |
| ear_baseline | 0.15 – 0.42 | 0.28 |
| mar_baseline | 0.20 – 0.70 | 0.45 |
| asymmetry_base | 0.005 – 0.15 | 0.03 |
| Solo calibra con | estabilidad ≥ 0.60 y cámara caliente | — |
| Persistencia | Se restaura en boot; máx. 7 días | `SOMNO_CALIB_MAX_AGE_S` |

---

## 14. Tiempos de respuesta (cierre de ojos sostenido)

| t | Evento |
|---|---|
| **0.5 s** | Detección del cierre (microsueño) |
| **~1.5 s** | Buzzer suena (SOMNOLENCIA) |
| **4 s** | EMERGENCIA — buzzer fijo |

**Latencia de procesamiento:** ~60–70 ms/frame (14–17 FPS) → cumple objetivo <100 ms.

---

## 15. Límites operativos

- **Cámara:** de frente, a la altura de los ojos (yaw ≈ 0). Fuera de frontal la pose no es confiable.
- **Luz:** brillo de rostro ≥ 0.30 para detección confiable; mínimo 0.12; sin IR no opera en oscuridad total.
- **Distancia:** ~45–50 cm (calibrar en la posición real de uso).

---

## 16. Configuración `.env` vigente

```ini
CAMERA_ROTATION=0
SOMNO_BUZZER_FREQ=2700
SOMNO_BUZZER_DUTY=50
SOMNO_RECOVERY_GRACE_S=3
CALIBRATION_SECONDS=90
SOMNO_FACE_QUALITY_MIN=0.6
SOMNO_RECOVERY_PER_S=8
SOMNO_RISE_GAIN=0.5
SOMNO_RESTORE_SCORE_CAP=0
SOMNO_PERCLOS_ONSET=0.25
SOMNO_EYE_CLOSED_EMERGENCY_MS=4000
SOMNO_HEAD_DOWN_EMERGENCY=0
SOMNO_RULES_ENABLED=0
SOMNO_SOUND_DELAY_S=0.1
```
