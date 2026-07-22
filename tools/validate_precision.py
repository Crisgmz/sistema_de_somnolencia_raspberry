#!/usr/bin/env python3
"""Arnes de validacion de precision de la deteccion de somnolencia.

Mide precision / recall / F1 / especificidad del sistema contra una verdad de
referencia etiquetada, y compara el nivel CRUDO (raw) contra el COMPROMETIDO
(committed) para cuantificar cuanto sube la precision el endurecimiento
(corroboracion + persistencia + histeresis).

Uso tipico
----------
1) Grabar una sesion con el sistema:
       SOMNO_RECORD_SESSION=1 ./run.sh
   Esto crea recordings/<session_id>.jsonl

2) Etiquetar los intervalos de somnolencia real en un CSV (labels.csv):
       start,end,drowsy
       0,120,0            # primeros 2 min: despierto
       120,300,1          # min 2-5: somnoliento (auto-reporte KSS>=7)
   (start/end en segundos relativos al inicio, epoch, o ISO8601)

3) Validar:
       ./tools/validate_precision.py --rec recordings/<session_id>.jsonl \
                                     --labels labels.csv

También puede leer de Supabase:  --source supabase --session <session_id>

El "positivo" del sistema se define como committed_level >= --alarm-level
(por defecto 2 = SOMNOLENCIA). El positivo de referencia es drowsy==1.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

# Permitir importar el proyecto cuando se ejecuta desde tools/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _parse_ts(raw: str) -> float:
    raw = str(raw).strip()
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    raise ValueError(f"Timestamp no reconocido: {raw!r}")


def load_records_jsonl(path: str) -> List[Dict]:
    records: List[Dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    records.sort(key=lambda r: float(r.get("ts", 0.0)))
    return records


def load_records_supabase(session_id: str) -> List[Dict]:
    from dotenv import load_dotenv
    from supabase import create_client
    from core.config import AppConfig

    load_dotenv()
    cfg = AppConfig.from_env()
    sb = create_client(cfg.supabase_url, cfg.supabase_key)
    rows = (
        sb.table("telemetry_raw")
        .select("payload")
        .eq("session_id", session_id)
        .order("id")
        .limit(100000)
        .execute()
    )
    records: List[Dict] = []
    for row in rows.data or []:
        payload = row.get("payload") or {}
        d = payload.get("drowsiness", {})
        score = payload.get("score", {})
        records.append(
            {
                "ts": float(payload.get("ts", 0.0)),
                "committed_level": int(d.get("committed_level", score.get("level", 0))),
                "raw_level": int(d.get("raw_level", score.get("level", 0))),
                "fatigue_score": int(d.get("fatigue_score", score.get("fatigue_score", 0))),
                "corroborated": bool(d.get("corroborated", True)),
                "face_quality_ok": bool(d.get("face_quality_ok", True)),
                "emergency": bool(payload.get("emergency", {}).get("emergencyflag", False)),
            }
        )
    records.sort(key=lambda r: r["ts"])
    return records


def load_labels(path: str, base_ts: float) -> List[Tuple[float, float, int]]:
    """Devuelve lista de (start_ts, end_ts, drowsy) en epoch absoluto."""
    intervals: List[Tuple[float, float, int]] = []
    with open(path, "r", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fields = {c.lower(): c for c in (reader.fieldnames or [])}
        for row in reader:
            def get(*names, default=""):
                for n in names:
                    if n in fields:
                        return row[fields[n]]
                return default

            start_raw = get("start", "start_ts", "from")
            end_raw = get("end", "end_ts", "to")
            label_raw = get("drowsy", "label", "y", "kss", default="0")
            try:
                start = _parse_ts(start_raw)
                end = _parse_ts(end_raw)
            except ValueError:
                continue
            # start/end pueden ser relativos al inicio de la grabacion.
            if start < 1e6:
                start += base_ts
            if end < 1e6:
                end += base_ts
            try:
                lv = float(label_raw)
            except ValueError:
                lv = 0.0
            drowsy = 1 if lv >= 1.0 else 0
            intervals.append((start, end, drowsy))
    return intervals


def label_for(ts: float, intervals: List[Tuple[float, float, int]]) -> Optional[int]:
    for start, end, drowsy in intervals:
        if start <= ts <= end:
            return drowsy
    return None


def confusion(y_true: List[int], y_pred: List[int]) -> Dict[str, float]:
    tp = sum(1 for t, p in zip(y_true, y_pred) if t == 1 and p == 1)
    fp = sum(1 for t, p in zip(y_true, y_pred) if t == 0 and p == 1)
    tn = sum(1 for t, p in zip(y_true, y_pred) if t == 0 and p == 0)
    fn = sum(1 for t, p in zip(y_true, y_pred) if t == 1 and p == 0)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    specificity = tn / (tn + fp) if (tn + fp) else 0.0
    accuracy = (tp + tn) / max(1, tp + tn + fp + fn)
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "precision": precision, "recall": recall,
        "specificity": specificity, "accuracy": accuracy, "f1": f1,
        "n": tp + tn + fp + fn,
    }


def _fmt(m: Dict[str, float]) -> str:
    return (
        f"P={m['precision']*100:5.1f}%  R={m['recall']*100:5.1f}%  "
        f"F1={m['f1']*100:5.1f}%  Esp={m['specificity']*100:5.1f}%  "
        f"Acc={m['accuracy']*100:5.1f}%  (TP={m['tp']} FP={m['fp']} TN={m['tn']} FN={m['fn']}, n={m['n']})"
    )


def evaluate(records: List[Dict], intervals: List[Tuple[float, float, int]], alarm_level: int) -> None:
    y_true: List[int] = []
    pred_committed: List[int] = []
    pred_raw: List[int] = []
    unlabeled = 0
    for r in records:
        gt = label_for(float(r.get("ts", 0.0)), intervals)
        if gt is None:
            unlabeled += 1
            continue
        y_true.append(gt)
        pred_committed.append(1 if int(r.get("committed_level", 0)) >= alarm_level else 0)
        pred_raw.append(1 if int(r.get("raw_level", 0)) >= alarm_level else 0)

    n = len(y_true)
    print("=" * 74)
    print(f"Registros: {len(records)} | etiquetados: {n} | sin etiqueta: {unlabeled}")
    print(f"Positivos de referencia (drowsy): {sum(y_true)} | negativos: {n - sum(y_true)}")
    print(f"Umbral de alarma del sistema: committed_level >= {alarm_level} ({_level_name(alarm_level)})")
    print("-" * 74)
    if n == 0:
        print("No hay registros etiquetados. Revisa el CSV de labels y los timestamps.")
        return
    m_committed = confusion(y_true, pred_committed)
    m_raw = confusion(y_true, pred_raw)
    print(f"COMMITTED (endurecido): {_fmt(m_committed)}")
    print(f"RAW (score crudo)     : {_fmt(m_raw)}")
    gain = (m_committed["precision"] - m_raw["precision"]) * 100
    print("-" * 74)
    print(f"Ganancia de precision por el endurecimiento: {gain:+.1f} pts")
    verdict = "SI" if m_committed["precision"] >= 0.95 else "NO"
    print(f"Precision COMMITTED >= 95%: {verdict} ({m_committed['precision']*100:.1f}%)")
    print("=" * 74)

    print("\nBarrido de umbral de alarma (committed_level):")
    print(f"  {'nivel':>5} | {'precision':>9} | {'recall':>7} | {'F1':>6} | {'especif':>7}")
    for lvl in range(1, 5):
        pred = [1 if int(r.get("committed_level", 0)) >= lvl else 0 for r in records if label_for(float(r.get('ts', 0.0)), intervals) is not None]
        m = confusion(y_true, pred)
        print(f"  {lvl:>5} | {m['precision']*100:8.1f}% | {m['recall']*100:6.1f}% | {m['f1']*100:5.1f}% | {m['specificity']*100:6.1f}%")


def _level_name(lvl: int) -> str:
    return ["NORMAL", "FATIGA", "SOMNOLENCIA", "CRITICO", "EMERGENCIA"][max(0, min(4, lvl))]


def main() -> None:
    ap = argparse.ArgumentParser(description="Validacion de precision de somnolencia")
    ap.add_argument("--rec", help="Ruta a recordings/<session>.jsonl")
    ap.add_argument("--source", choices=["jsonl", "supabase"], default="jsonl")
    ap.add_argument("--session", help="session_id (para --source supabase)")
    ap.add_argument("--labels", required=True, help="CSV de etiquetas (start,end,drowsy)")
    ap.add_argument("--alarm-level", type=int, default=2, help="Nivel minimo que cuenta como alarma (default 2=SOMNOLENCIA)")
    args = ap.parse_args()

    if args.source == "supabase":
        if not args.session:
            ap.error("--source supabase requiere --session")
        records = load_records_supabase(args.session)
    else:
        if not args.rec:
            ap.error("--source jsonl requiere --rec")
        records = load_records_jsonl(args.rec)

    if not records:
        print("No se cargaron registros.")
        return
    base_ts = float(records[0].get("ts", 0.0))
    intervals = load_labels(args.labels, base_ts)
    if not intervals:
        print("No se cargaron intervalos de etiquetas.")
        return
    evaluate(records, intervals, args.alarm_level)


if __name__ == "__main__":
    main()
