#!/usr/bin/env python3
"""Evaluacion formal del sistema: matriz de confusion y metricas sobre N pruebas.

Flujo:
  1. Generar la plantilla de pruebas (200 filas balanceadas):
         python tools/evaluar_matriz.py --plantilla pruebas_200.csv
  2. Ejecutar cada prueba con el sistema en vivo y anotar en el CSV la columna
     `prediccion` (ALERTA si el sistema marco nivel >=2 o emergencia dentro de
     la ventana de la prueba; NO_ALERTA si no).
  3. Calcular matriz y metricas:
         python tools/evaluar_matriz.py pruebas_200.csv
  4. Actualizar el reporte (reescribe la seccion marcada):
         python tools/evaluar_matriz.py pruebas_200.csv --reporte docs/REPORTE_EVALUACION_200_PRUEBAS.md

Convencion (clase positiva = SOMNOLIENTO):
  TP: real SOMNOLIENTO y prediccion ALERTA      (deteccion correcta)
  FN: real SOMNOLIENTO y prediccion NO_ALERTA   (somnolencia NO detectada - el fallo peligroso)
  FP: real NORMAL      y prediccion ALERTA      (falsa alarma)
  TN: real NORMAL      y prediccion NO_ALERTA   (correcto silencio)

  Accuracy      = (TP+TN)/N
  Precision     = TP/(TP+FP)
  Recall        = TP/(TP+FN)   (sensibilidad: % de somnolencias detectadas)
  Especificidad = TN/(TN+FP)
  F1            = 2*P*R/(P+R)
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import date

REAL_POS = "SOMNOLIENTO"
REAL_NEG = "NORMAL"
PRED_POS = "ALERTA"
PRED_NEG = "NO_ALERTA"

# Escenarios sugeridos para las 200 pruebas (10 grupos x 20 repeticiones).
# La mitad son casos POSITIVOS (el sistema DEBE alertar) y la otra mitad
# NEGATIVOS (el sistema NO debe alertar), cubriendo los confusores tipicos.
ESCENARIOS = [
    (REAL_POS, "microsueno: ojos cerrados 2-3 s, de frente"),
    (REAL_POS, "microsueno con lentes: ojos cerrados 2-3 s"),
    (REAL_POS, "somnolencia gradual: parpadeo pesado + cierres >1 s repetidos (PERCLOS)"),
    (REAL_POS, "cabeceo: caida de cabeza sostenida >=3 s"),
    (REAL_POS, "bostezo sostenido >=2 s (con y sin mano tapando parcialmente)"),
    (REAL_NEG, "conduccion normal: mirada al frente, parpadeo natural"),
    (REAL_NEG, "conduccion normal con lentes"),
    (REAL_NEG, "hablar/reir/cantar (movimiento de boca sin bostezo)"),
    (REAL_NEG, "mirar espejos/tablero: giros de cabeza breves"),
    (REAL_NEG, "poca luz o contraluz, conductor despierto"),
]


def generar_plantilla(path: str, n: int) -> None:
    por_grupo = max(1, n // len(ESCENARIOS))
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "escenario", "condicion_real", "prediccion", "notas"])
        i = 1
        for real, descripcion in ESCENARIOS:
            for _ in range(por_grupo):
                w.writerow([i, descripcion, real, "", ""])
                i += 1
    print(f"[OK] Plantilla con {i - 1} pruebas en {path}")
    print("Completa la columna 'prediccion' con ALERTA o NO_ALERTA tras cada prueba.")


def leer_pruebas(path: str):
    filas = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            real = (row.get("condicion_real") or "").strip().upper()
            pred = (row.get("prediccion") or "").strip().upper()
            if not pred:
                continue  # prueba aun no ejecutada
            if real not in (REAL_POS, REAL_NEG) or pred not in (PRED_POS, PRED_NEG):
                print(f"[WARN] Fila {row.get('id')}: valores invalidos ({real!r}, {pred!r}); ignorada")
                continue
            filas.append((row.get("id", "?"), (row.get("escenario") or "").strip(), real, pred))
    return filas


def metricas(tp: int, fp: int, fn: int, tn: int) -> dict:
    n = tp + fp + fn + tn
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    return {
        "n": n,
        "accuracy": (tp + tn) / n if n else 0.0,
        "precision": prec,
        "recall": rec,
        "especificidad": tn / (tn + fp) if (tn + fp) else 0.0,
        "f1": 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0,
    }


def texto_resultados(filas) -> str:
    tp = sum(1 for _, _, r, p in filas if r == REAL_POS and p == PRED_POS)
    fn = sum(1 for _, _, r, p in filas if r == REAL_POS and p == PRED_NEG)
    fp = sum(1 for _, _, r, p in filas if r == REAL_NEG and p == PRED_POS)
    tn = sum(1 for _, _, r, p in filas if r == REAL_NEG and p == PRED_NEG)
    m = metricas(tp, fp, fn, tn)

    out = []
    out.append(f"Pruebas evaluadas: {m['n']}  (fecha de calculo: {date.today().isoformat()})")
    out.append("")
    out.append("### Matriz de confusion")
    out.append("")
    out.append("| | Prediccion: ALERTA | Prediccion: NO ALERTA |")
    out.append("|---|---|---|")
    out.append(f"| **Real: SOMNOLIENTO** | VP = {tp} | FN = {fn} |")
    out.append(f"| **Real: NORMAL** | FP = {fp} | VN = {tn} |")
    out.append("")
    out.append("### Metricas")
    out.append("")
    out.append("| Metrica | Formula | Valor |")
    out.append("|---|---|---|")
    out.append(f"| Accuracy | (VP+VN)/N | **{m['accuracy']:.4f}** ({m['accuracy']*100:.2f}%) |")
    out.append(f"| Precision | VP/(VP+FP) | **{m['precision']:.4f}** ({m['precision']*100:.2f}%) |")
    out.append(f"| Recall (sensibilidad) | VP/(VP+FN) | **{m['recall']:.4f}** ({m['recall']*100:.2f}%) |")
    out.append(f"| Especificidad | VN/(VN+FP) | **{m['especificidad']:.4f}** ({m['especificidad']*100:.2f}%) |")
    out.append(f"| F1-score | 2·P·R/(P+R) | **{m['f1']:.4f}** |")
    out.append("")

    # Desglose por escenario (si hay descripcion).
    grupos: dict[str, list] = defaultdict(list)
    for _, esc, r, p in filas:
        if esc:
            grupos[esc].append((r, p))
    if grupos:
        out.append("### Desglose por escenario")
        out.append("")
        out.append("| Escenario | Pruebas | Aciertos | Tasa |")
        out.append("|---|---|---|---|")
        for esc, items in grupos.items():
            ok = sum(
                1 for r, p in items
                if (r == REAL_POS and p == PRED_POS) or (r == REAL_NEG and p == PRED_NEG)
            )
            out.append(f"| {esc} | {len(items)} | {ok} | {ok / len(items) * 100:.0f}% |")
        out.append("")
    return "\n".join(out)


MARCA_INICIO = "<!-- RESULTADOS:INICIO -->"
MARCA_FIN = "<!-- RESULTADOS:FIN -->"


def actualizar_reporte(reporte_path: str, bloque: str) -> None:
    with open(reporte_path, encoding="utf-8") as fh:
        contenido = fh.read()
    if MARCA_INICIO not in contenido or MARCA_FIN not in contenido:
        print(f"[ERROR] {reporte_path} no tiene los marcadores {MARCA_INICIO} / {MARCA_FIN}")
        sys.exit(1)
    antes = contenido.split(MARCA_INICIO)[0]
    despues = contenido.split(MARCA_FIN)[1]
    nuevo = f"{antes}{MARCA_INICIO}\n{bloque}\n{MARCA_FIN}{despues}"
    with open(reporte_path, "w", encoding="utf-8") as fh:
        fh.write(nuevo)
    print(f"[OK] Seccion de resultados actualizada en {reporte_path}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Matriz de confusion y metricas del sistema de somnolencia")
    ap.add_argument("csv", nargs="?", help="CSV de pruebas ejecutadas")
    ap.add_argument("--plantilla", metavar="RUTA", help="genera la plantilla CSV de pruebas y sale")
    ap.add_argument("--n", type=int, default=200, help="numero de pruebas de la plantilla (default 200)")
    ap.add_argument("--reporte", metavar="RUTA", help="actualiza la seccion de resultados del reporte MD")
    args = ap.parse_args()

    if args.plantilla:
        generar_plantilla(args.plantilla, args.n)
        return 0
    if not args.csv:
        ap.print_help()
        return 1
    filas = leer_pruebas(args.csv)
    if not filas:
        print("[ERROR] El CSV no tiene pruebas con 'prediccion' completada.")
        return 1
    bloque = texto_resultados(filas)
    print(bloque)
    if args.reporte:
        actualizar_reporte(args.reporte, bloque)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
