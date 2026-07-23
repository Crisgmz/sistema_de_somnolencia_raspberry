"""Control de buzzer GPIO por nivel de alerta.

Soporta buzzer PASIVO (piezo sin oscilador propio): el tono se genera por PWM a
una frecuencia audible. Un buzzer pasivo NO suena con solo poner el pin en HIGH
(corriente continua); necesita una senal alterna (PWM) para vibrar. La frecuencia
y el ciclo de trabajo (volumen aparente) son configurables por entorno:

    SOMNO_BUZZER_FREQ   frecuencia del tono en Hz (default 2700, cerca de la
                        resonancia tipica de un piezo -> maximo volumen)
    SOMNO_BUZZER_DUTY   ciclo de trabajo 1-99 % (default 50)

Si el PWM no esta disponible (o el buzzer es ACTIVO), se degrada a on/off simple.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Dict, Tuple

try:
    import RPi.GPIO as GPIO
except Exception:
    GPIO = None


def _env_f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


class Buzzer:
    PATTERNS: Dict[int, Tuple[float, float]] = {
        0: (0.0, 0.0),
        1: (0.08, 0.92),  # leve: 1 beep/s
        2: (0.12, 0.48),  # moderado: 2 beeps/s aprox
        3: (0.16, 0.24),  # critico: mas rapido
        4: (0.22, 0.08),  # emergencia: casi continuo
    }
    LEVEL_LABELS: Dict[int, str] = {
        0: "NORMAL",
        1: "FATIGA",
        2: "SOMNOLENCIA",
        3: "CRITICO",
        4: "EMERGENCIA",
    }

    def __init__(self, pin: int = 17, active_high: bool = True, enabled: bool = True) -> None:
        self.pin = int(pin)
        self.active_high = bool(active_high)
        self.enabled = bool(enabled) and GPIO is not None
        # Tono del buzzer pasivo. La frecuencia optima depende del piezo; 2700 Hz
        # es un buen punto de partida. Si suena flojo, prueba 2000-4000 Hz.
        self._freq = max(50.0, _env_f("SOMNO_BUZZER_FREQ", 2700.0))
        self._duty = min(99.0, max(1.0, _env_f("SOMNO_BUZZER_DUTY", 50.0)))
        self._pwm = None
        self._use_pwm = False
        self._stop = threading.Event()
        self._level = 0
        self._continuous = False
        self._last_logged_level = -1
        # Chirp one-shot (p.ej. distraccion): patron breve y distinto del de
        # fatiga que se reproduce una vez y luego vuelve al modo por nivel.
        self._chirp_lock = threading.Lock()
        self._chirp: Tuple[int, float, float] | None = None
        self._thread = threading.Thread(target=self._worker, daemon=True)
        if self.enabled:
            try:
                GPIO.setmode(GPIO.BCM)
                GPIO.setup(self.pin, GPIO.OUT)
                # Se intenta PWM (necesario para buzzer pasivo). Si falla, se usa
                # on/off simple (buzzer activo).
                try:
                    self._pwm = GPIO.PWM(self.pin, self._freq)
                    self._pwm.start(0.0)  # arranca en silencio (duty 0)
                    self._use_pwm = True
                    print(f"[BUZZER] PWM activo pin={self.pin} freq={self._freq:.0f}Hz duty={self._duty:.0f}%")
                except Exception as exc_pwm:
                    self._use_pwm = False
                    print(f"[BUZZER] Sin PWM ({exc_pwm}); usando on/off simple")
            except Exception as exc:
                self.enabled = False
                print(f"[WARN] Buzzer deshabilitado por error GPIO: {exc}")
        self._thread.start()

    def _tone(self, on: bool) -> None:
        """Enciende (tono) o apaga el buzzer."""
        if not self.enabled:
            return
        try:
            if self._use_pwm and self._pwm is not None:
                # Tono via PWM: duty configurado = sonando; duty 0 = silencio.
                self._pwm.ChangeDutyCycle(self._duty if on else 0.0)
            else:
                # Buzzer activo: nivel logico segun active_high.
                if self.active_high:
                    GPIO.output(self.pin, GPIO.HIGH if on else GPIO.LOW)
                else:
                    GPIO.output(self.pin, GPIO.LOW if on else GPIO.HIGH)
        except Exception:
            self.enabled = False

    def set_level(self, level: int) -> None:
        new_level = max(0, min(4, int(level)))
        self._level = new_level
        if new_level != self._last_logged_level:
            on_s, off_s = self.PATTERNS[new_level]
            print(f"[BUZZER] Nivel {new_level} ({self.LEVEL_LABELS[new_level]}) | patron on={on_s:.2f}s off={off_s:.2f}s")
            self._last_logged_level = new_level

    def set_continuous(self, enabled: bool) -> None:
        new_mode = bool(enabled)
        if new_mode != self._continuous:
            print(f"[BUZZER] Modo fijo {'ON' if new_mode else 'OFF'}")
        self._continuous = new_mode

    def chirp(self, beeps: int = 2, on_s: float = 0.05, off_s: float = 0.05) -> None:
        """Solicita un patron breve one-shot (no altera el nivel de fatiga)."""
        with self._chirp_lock:
            self._chirp = (max(1, int(beeps)), float(on_s), float(off_s))

    def _take_chirp(self) -> "Tuple[int, float, float] | None":
        with self._chirp_lock:
            c = self._chirp
            self._chirp = None
        return c

    def _worker(self) -> None:
        while not self._stop.is_set():
            chirp = self._take_chirp()
            if chirp is not None:
                beeps, on_s, off_s = chirp
                for _ in range(beeps):
                    if self._stop.is_set():
                        break
                    self._tone(True)
                    self._stop.wait(on_s)
                    self._tone(False)
                    self._stop.wait(off_s)
                continue
            if self._continuous and self._level > 0:
                self._tone(True)
                self._stop.wait(0.05)
                continue
            on_s, off_s = self.PATTERNS[self._level]
            if on_s <= 0.0:
                self._tone(False)
                self._stop.wait(0.15)
                continue
            self._tone(True)
            self._stop.wait(on_s)
            self._tone(False)
            self._stop.wait(off_s)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        self._tone(False)
        if self.enabled:
            try:
                if self._use_pwm and self._pwm is not None:
                    self._pwm.stop()
            except Exception:
                pass
            try:
                GPIO.cleanup(self.pin)
            except Exception:
                pass
