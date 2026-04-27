import glob
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


def list_video_devices() -> list[str]:
    return sorted(glob.glob("/dev/video*"))


def list_opencv_candidates() -> list[str]:
    if not shutil.which("v4l2-ctl"):
        return []

    try:
        proc = subprocess.run(
            ["v4l2-ctl", "--list-devices"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return []

    if proc.returncode != 0 and not proc.stdout:
        return []

    excluded_tokens = ("bcm2835-codec", "bcm2835-isp", "rpi-hevc-dec")
    candidates: list[str] = []
    current_header = ""
    for raw_line in proc.stdout.splitlines():
        line = raw_line.rstrip()
        if not line:
            current_header = ""
            continue
        if not raw_line.startswith("\t"):
            current_header = line.lower()
            continue
        if any(token in current_header for token in excluded_tokens):
            continue
        device_path = line.strip()
        if device_path.startswith("/dev/video"):
            candidates.append(device_path)

    unique: list[str] = []
    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique.append(candidate)
    return unique


def list_picamera_cameras() -> list[dict]:
    try:
        return Picamera2.global_camera_info()
    except Exception:
        return []


def describe_camera_environment() -> str:
    parts = []

    picam_info = list_picamera_cameras()
    if picam_info:
        summary = ", ".join(
            f"Num={cam.get('Num', '?')}:{cam.get('Id', cam.get('Model', 'sin_id'))}"
            for cam in picam_info
        )
        parts.append(f"libcamera={len(picam_info)} [{summary}]")
    else:
        parts.append("libcamera=0")

    video_nodes = list_video_devices()
    parts.append("video_nodes=" + (", ".join(video_nodes) if video_nodes else "ninguno"))
    opencv_candidates = list_opencv_candidates()
    parts.append("opencv_candidates=" + (", ".join(opencv_candidates) if opencv_candidates else "ninguno"))
    return " | ".join(parts)


def setup_camera(max_retries=6, retry_delay=0.8):
    print("-> Configurando imagen de Arducam...")

    picam_info = list_picamera_cameras()
    if not picam_info:
        raise RuntimeError(
            "Picamera2/libcamera no detecta ninguna camara. "
            + describe_camera_environment()
        )

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            picam2 = Picamera2()
            config = picam2.create_video_configuration(main={"size": (640, 480), "format": "BGR888"})
            picam2.configure(config)
            picam2.start()
            break
        except (RuntimeError, IndexError) as exc:
            last_error = exc
            if attempt < max_retries:
                print(f"-> Cámara ocupada o no disponible (intento {attempt}/{max_retries}). Reintentando...")
                time.sleep(retry_delay)
                continue

            users = _find_camera_users()
            message = [
                "No se pudo adquirir la cámara después de varios intentos.",
                f"Estado detectado: {describe_camera_environment()}",
                "Causa probable: otro proceso está usando la cámara o libcamera no la expone correctamente.",
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
