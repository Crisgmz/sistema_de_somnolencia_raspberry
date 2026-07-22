"""Tests del LevelStabilizer: corroboracion multi-cue, persistencia e histeresis."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.corroboration import LevelStabilizer, StabilizerConfig, family_of


def _run(st, level, params, t0, seconds, step=0.1, forced=0):
    out = None
    t = t0
    while t < t0 + seconds:
        out = st.update(t, level, params, forced_min_level=forced)
        t += step
    return out, t


def test_pico_aislado_no_compromete():
    """Un pico de 1 frame de nivel 4 NO debe comprometer un nivel alto."""
    st = LevelStabilizer()
    st.update(0.0, 4, ["PERCLOS", "PITCH"])
    out = st.update(0.05, 0, [])
    assert out["committed_level"] == 0, out


def test_persistencia_sube_con_evidencia_sostenida():
    """Evidencia corroborada y sostenida sube de nivel escalonadamente."""
    st = LevelStabilizer(StabilizerConfig(t_up_s=(0.0, 0.5, 0.5, 0.5, 0.5)))
    out, t = _run(st, 3, ["PERCLOS", "PITCH"], 0.0, 3.0)
    assert out["committed_level"] == 3, out
    assert out["corroborated"] is True


def test_una_sola_familia_se_limita_sin_microsueno():
    """Nivel alto con una sola familia (sin microsueno) se topa en el cap."""
    st = LevelStabilizer(StabilizerConfig(t_up_s=(0.0, 0.3, 0.3, 0.3, 0.3), single_family_level_cap=2))
    # Solo cabeza (PITCH), sin evidencia ocular fuerte.
    out, t = _run(st, 4, ["PITCH"], 0.0, 3.0)
    assert out["committed_level"] <= 2, out
    assert out["corroborated"] is False


def test_microsueno_solo_es_suficiente():
    """PERCLOS (ocular fuerte) por si solo puede superar el cap de una familia."""
    st = LevelStabilizer(StabilizerConfig(t_up_s=(0.0, 0.3, 0.3, 0.3, 0.3)))
    out, t = _run(st, 3, ["PERCLOS"], 0.0, 3.0)
    assert out["committed_level"] == 3, out
    assert out["strong_ocular"] is True


def test_histeresis_baja_mas_lento_que_un_frame():
    """Tras evidencia alta, un unico frame en 0 no derrumba el nivel de golpe."""
    st = LevelStabilizer(StabilizerConfig(t_up_s=(0.0, 0.3, 0.3, 0.3, 0.3), t_down_s=1.0))
    _run(st, 3, ["PERCLOS", "PITCH"], 0.0, 3.0)
    out = st.update(3.05, 0, [])  # un frame sin evidencia
    assert out["committed_level"] >= 2, out


def test_forced_min_level_es_piso():
    """El forzado por reglas actua como piso incluso sin evidencia sostenida."""
    st = LevelStabilizer()
    out = st.update(0.0, 0, [], forced_min_level=2)
    assert out["committed_level"] == 2, out


def test_family_map():
    assert family_of("PERCLOS") == "OJOS"
    assert family_of("MAR") == "BOCA"
    assert family_of("PITCH") == "CABEZA"
    assert family_of("DESCONOCIDO") == "OTRO"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{passed}/{len(fns)} tests OK")
    sys.exit(0 if passed == len(fns) else 1)
