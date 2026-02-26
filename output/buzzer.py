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
        1: (0.10, 0.90),
        2: (0.15, 0.45),
        3: (0.20, 0.20),
        4: (0.30, 0.05),
    }

    def __init__(self, pin: int = 17, active_high: bool = True, enabled: bool = True) -> None:
        self.pin = int(pin)
        self.active_high = bool(active_high)
        self.enabled = bool(enabled) and GPIO is not None
        self._stop = threading.Event()
        self._level = 0
        self._thread = threading.Thread(target=self._worker, daemon=True)
        if self.enabled:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.pin, GPIO.OUT)
        self._thread.start()

    def _write(self, on: bool) -> None:
        if not self.enabled:
            return
        if self.active_high:
            GPIO.output(self.pin, GPIO.HIGH if on else GPIO.LOW)
        else:
            GPIO.output(self.pin, GPIO.LOW if on else GPIO.HIGH)

    def set_level(self, level: int) -> None:
        self._level = max(0, min(4, int(level)))

    def _worker(self) -> None:
        while not self._stop.is_set():
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
            GPIO.cleanup(self.pin)
