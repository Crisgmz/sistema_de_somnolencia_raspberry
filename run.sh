#!/bin/bash
# Lanzador del sistema de deteccion de somnolencia.
#
# IMPORTANTE: NO uses `python3 main.py` directamente. El Python del sistema
# (3.13) no tiene las dependencias; todo vive en el venv de Python 3.12
# (.venv), que ademas trae picamera2 con autofoco/AWB. Este script usa ese
# interprete. Tampoco se necesita `libcamerify`: picamera2 habla con libcamera
# directamente (usarlo causaria "Multiple CameraManager objects are not allowed").

cd "$(dirname "$0")" || exit 1
exec .venv/bin/python main.py "$@"
