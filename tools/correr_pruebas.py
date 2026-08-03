#!/usr/bin/env python3
"""Corredor guiado de pruebas: llena la columna `prediccion` del CSV solo.

En una terminal corre el sistema con el grabador de sesion activo:
    SOMNO_RECORD_SESSION=1 ./run.sh

En OTRA terminal corre este script:
    python tools/correr_pruebas.py pruebas_200.csv

Por cada prueba pendiente:
  1. Muestra el escenario a simular y espera Enter para iniciar.
  2. Observa el JSONL en vivo durante la ventana (default 30 s).
  3. Registra ALERTA si el sistema comprometio nivel >= 2 o emergencia en la
     ventana; NO_ALERTA si no. Anota el nivel maximo visto en `notas`.
  4. Guarda el CSV tras CADA prueba (se puede interrumpir y retomar: solo
     procesa filas con `prediccion` vacia).

Controles tras cada prueba: [Enter] aceptar y seguir | r repetir | q salir.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys
import time

# Criterio de ALERTA para la matriz: cuenta como ALERTA si el nivel comprometido
# llega a 2 (SOMNOLENCIA), 3 (CRITICO) o 4 (EMERGENCIA), o si hay emergencia.
# NORMAL (0) y FATIGA (1) NO cuentan como alerta.
NIVEL_ALERTA = 2


def jsonl_mas_reciente(directorio: str) -> str | None:
    archivos = glob.glob(os.path.join(directorio, "*.jsonl"))
    if not archivos:
        return None
    return max(archivos, key=os.path.getmtime)


def evaluar_lineas(lineas: list[str]) -> tuple[str, int, bool]:
    """Evalua las lineas JSONL de la ventana: (prediccion, nivel_max, emergencia)."""
    nivel_max = 0
    emergencia = False
    for linea in lineas:
        try:
            d = json.loads(linea)
        except ValueError:
            continue
        nivel_max = max(nivel_max, int(d.get("committed_level", 0)))
        emergencia = emergencia or bool(d.get("emergency", False))
    pred = "ALERTA" if (nivel_max >= NIVEL_ALERTA or emergencia) else "NO_ALERTA"
    return pred, nivel_max, emergencia


def observar_ventana(jsonl_path: str, duracion_s: float) -> tuple[str, int, bool]:
    """Sigue el JSONL en vivo durante `duracion_s` y evalua lo escrito."""
    lineas: list[str] = []
    with open(jsonl_path, encoding="utf-8") as fh:
        fh.seek(0, os.SEEK_END)  # solo lo que se escriba desde ahora
        fin = time.monotonic() + duracion_s
        while time.monotonic() < fin:
            linea = fh.readline()
            if linea:
                lineas.append(linea)
                restante = int(fin - time.monotonic())
                print(f"\r  observando... {restante:3d}s restantes ({len(lineas)} muestras)", end="", flush=True)
            else:
                time.sleep(0.2)
    print()
    return evaluar_lineas(lineas)


def main() -> int:
    ap = argparse.ArgumentParser(description="Corredor guiado: llena 'prediccion' desde el JSONL en vivo")
    ap.add_argument("csv", help="CSV de pruebas (de tools/evaluar_matriz.py --plantilla)")
    ap.add_argument("--jsonl", help="JSONL de sesion en vivo (default: el mas reciente en recordings/)")
    ap.add_argument("--duracion", type=float, default=10.0, help="segundos por prueba (default 10)")
    args = ap.parse_args()

    jsonl_path = args.jsonl or jsonl_mas_reciente(os.getenv("SOMNO_RECORD_DIR", "recordings"))
    if not jsonl_path or not os.path.exists(jsonl_path):
        print("[ERROR] No hay JSONL de sesion. Arranca el sistema con SOMNO_RECORD_SESSION=1 ./run.sh")
        return 1
    edad_s = time.time() - os.path.getmtime(jsonl_path)
    if edad_s > 30:
        print(f"[WARN] {jsonl_path} lleva {edad_s:.0f}s sin escribirse. ¿El sistema esta corriendo con SOMNO_RECORD_SESSION=1?")
    print(f"[INFO] Observando sesion: {jsonl_path} | ventana por prueba: {args.duracion:.0f}s")

    with open(args.csv, newline="", encoding="utf-8") as fh:
        lector = csv.DictReader(fh)
        campos = lector.fieldnames or []
        filas = list(lector)
    pendientes = [f for f in filas if not (f.get("prediccion") or "").strip()]
    print(f"[INFO] {len(pendientes)} pruebas pendientes de {len(filas)}\n")

    def guardar() -> None:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=campos)
            w.writeheader()
            w.writerows(filas)

    hechas = 0
    for fila in filas:
        if (fila.get("prediccion") or "").strip():
            continue
        esperado = "debe ALERTAR" if fila["condicion_real"] == "SOMNOLIENTO" else "NO debe alertar"
        print(f"── Prueba {fila['id']}/{len(filas)} ── {fila['escenario']}")
        print(f"   Condicion real: {fila['condicion_real']} ({esperado})")
        while True:
            cmd = input("   [Enter] iniciar | q salir: ").strip().lower()
            if cmd == "q":
                guardar()
                print(f"[INFO] Sesion pausada. {hechas} pruebas registradas; retoma con el mismo comando.")
                return 0
            pred, nivel_max, emergencia = observar_ventana(jsonl_path, args.duracion)
            correcto = (fila["condicion_real"] == "SOMNOLIENTO") == (pred == "ALERTA")
            marca = "ACIERTO" if correcto else "FALLO"
            print(f"   -> {pred} (nivel max {nivel_max}{', EMERGENCIA' if emergencia else ''}) [{marca}]")
            cmd = input("   [Enter] aceptar | r repetir | q salir sin guardar esta: ").strip().lower()
            if cmd == "r":
                continue
            if cmd == "q":
                guardar()
                print(f"[INFO] Sesion pausada. {hechas} pruebas registradas.")
                return 0
            fila["prediccion"] = pred
            fila["notas"] = f"nivel_max={nivel_max}" + (";emergencia" if emergencia else "")
            hechas += 1
            guardar()
            break
        print()

    print(f"[OK] Todas las pruebas completadas ({hechas} nuevas). Calcula las metricas con:")
    print(f"  python tools/evaluar_matriz.py {args.csv} --reporte docs/REPORTE_EVALUACION_200_PRUEBAS.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
