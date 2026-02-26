import shutil
import subprocess
import time

from picamera2 import Picamera2


def _find_camera_users():
    checks = [
        ["fuser", "-v", "/dev/video0"],
        ["fuser", "-v", "/dev/media0"],
    ]
    details = []
    for cmd in checks:
        tool = cmd[0]
        if not shutil.which(tool):
            continue
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
            out = (proc.stdout + "\n" + proc.stderr).strip()
            if out:
                details.append(f"$ {' '.join(cmd)}\n{out}")
        except Exception:
            continue
    return "\n\n".join(details).strip()


def setup_camera(max_retries=6, retry_delay=0.8):
    print("-> Configurando imagen de Arducam...")
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            picam2 = Picamera2()
            config = picam2.create_video_configuration(main={"size": (640, 480)})
            picam2.configure(config)
            picam2.start()
            break
        except RuntimeError as exc:
            last_error = exc
            if attempt < max_retries:
                print(f"-> Cámara ocupada o no disponible (intento {attempt}/{max_retries}). Reintentando...")
                time.sleep(retry_delay)
                continue

            users = _find_camera_users()
            message = [
                "No se pudo adquirir la cámara después de varios intentos.",
                "Causa probable: otro proceso está usando la cámara.",
                "Cierra apps como `libcamera-hello`, `rpicam-*`, otro script de Python o reinicia el servicio de cámara.",
            ]
            if users:
                message.append("Procesos detectados usando la cámara:\n" + users)
            raise RuntimeError("\n".join(message)) from last_error

    # Perfil visual neutro para evitar apariencia "filtrada".
    picam2.set_controls(
        {
            "AfMode": 2,
            "AeExposureMode": 0,
            "AwbMode": 0,
            "Brightness": 0.0,
            "Contrast": 1.0,
        }
    )
    return picam2
