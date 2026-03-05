"""Control de buzzer GPIO por nivel de alerta."""

from __future__ import annotations

import threading
import time
from typing import Dict, Tuple

try:
    import RPi.GPIO as GPIO
except Exception:
    GPIO = None


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
        self._stop = threading.Event()
        self._level = 0
        self._continuous = False
        self._last_logged_level = -1
        self._thread = threading.Thread(target=self._worker, daemon=True)
        if self.enabled:
            try:
                GPIO.setmode(GPIO.BCM)
                GPIO.setup(self.pin, GPIO.OUT)
            except Exception as exc:
                self.enabled = False
                print(f"[WARN] Buzzer deshabilitado por error GPIO: {exc}")
        self._thread.start()

    def _write(self, on: bool) -> None:
        if not self.enabled:
            return
        try:
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

    def _worker(self) -> None:
        while not self._stop.is_set():
            if self._continuous and self._level > 0:
                self._write(True)
                self._stop.wait(0.05)
                continue
            on_s, off_s = self.PATTERNS[self._level]
            if on_s <= 0.0:
                self._write(False)
                self._stop.wait(0.15)
                continue
            self._write(True)
            self._stop.wait(on_s)
            self._write(False)
            self._stop.wait(off_s)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        self._write(False)
        if self.enabled:
            try:
                GPIO.cleanup(self.pin)
            except Exception:
                pass
