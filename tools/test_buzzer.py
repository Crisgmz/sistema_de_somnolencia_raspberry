#!/usr/bin/env python3
"""Prueba del buzzer pasivo por PWM.

Barre varias frecuencias para que oigas cual suena MAS FUERTE en tu piezo (su
frecuencia de resonancia) y luego pruebes los patrones por nivel.

Uso (con el servicio DETENIDO para no chocar por el pin 17):
    systemctl --user stop somnolencia.service
    .venv/bin/python tools/test_buzzer.py
    # al terminar, reinicia el servicio:
    systemctl --user start somnolencia.service

Ajusta el pin con la variable de entorno SOMNO_BUZZER_PIN (default 17).
"""

import os
import time

import RPi.GPIO as GPIO

PIN = int(os.getenv("SOMNO_BUZZER_PIN", "17"))
DUTY = float(os.getenv("SOMNO_BUZZER_DUTY", "50"))
FREQS = [1000, 1500, 2000, 2400, 2700, 3000, 3500, 4000]


def main() -> None:
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(PIN, GPIO.OUT)
    pwm = GPIO.PWM(PIN, FREQS[0])
    pwm.start(0.0)
    try:
        print(f"Pin={PIN} duty={DUTY:.0f}%  (Ctrl+C para salir)\n")
        print("== Barrido de frecuencias: anota la que suene MAS FUERTE ==")
        for f in FREQS:
            print(f"  {f} Hz ...")
            pwm.ChangeFrequency(f)
            pwm.ChangeDutyCycle(DUTY)
            time.sleep(1.2)
            pwm.ChangeDutyCycle(0.0)
            time.sleep(0.3)

        best = int(os.getenv("SOMNO_BUZZER_FREQ", "2700"))
        print(f"\n== Patrones por nivel a {best} Hz ==")
        pwm.ChangeFrequency(best)
        patterns = {
            1: (0.08, 0.92),
            2: (0.12, 0.48),
            3: (0.16, 0.24),
            4: (0.22, 0.08),
        }
        for level, (on_s, off_s) in patterns.items():
            print(f"  Nivel {level}: on={on_s}s off={off_s}s (3s)")
            t_end = time.time() + 3.0
            while time.time() < t_end:
                pwm.ChangeDutyCycle(DUTY)
                time.sleep(on_s)
                pwm.ChangeDutyCycle(0.0)
                time.sleep(off_s)
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        pwm.ChangeDutyCycle(0.0)
        pwm.stop()
        GPIO.cleanup(PIN)
        print("\nListo.")


if __name__ == "__main__":
    main()
