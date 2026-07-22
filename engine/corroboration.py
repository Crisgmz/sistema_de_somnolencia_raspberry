"""Estabilizador de nivel de somnolencia: sube la precision del modelo actual.

Este modulo NO reemplaza el DynamicFatigueScore; lo *endurece*. Toma el nivel
"crudo" (raw) que produce el score aditivo cada frame y devuelve un nivel
"comprometido" (committed) aplicando tres mecanismos que, segun la literatura de
deteccion de fatiga, son las palancas que reducen falsos positivos:

1. Corroboracion multi-cue: para comprometer niveles altos (>=3 CRITICO) se
   exige evidencia de al menos DOS familias fisiologicas independientes
   (ojos / boca / cabeza) dentro de una ventana reciente, salvo que exista
   evidencia ocular fuerte de microsueno (PERCLOS / ojo cerrado prolongado /
   parpadeo muy lento), que por si sola es suficiente segun la evidencia.

2. Persistencia temporal: una subida de nivel solo se compromete si el nivel
   candidato se sostiene un tiempo minimo (T_up). Esto elimina los picos de
   1 frame (p.ej. un landmark ruidoso) que disparaban "EMERGENCIA".

3. Histeresis: las bajadas se aplican mas rapido que las subidas pero no de
   forma instantanea, para no oscilar. La seguridad manda: bajar rapido, subir
   con evidencia sostenida.

Todo es configurable por entorno y las clases son puras/testeables.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Iterable, List, Optional, Set, Tuple

# Familia fisiologica de cada parametro. Solo las familias "primarias"
# (OJOS/BOCA/CABEZA) cuentan para la corroboracion multi-cue; el resto son
# moduladores de contexto que por si solos no deben elevar el nivel.
PARAM_FAMILY: Dict[str, str] = {
    # Ojos
    "EAR": "OJOS",
    "EYE_CLOSED_MS": "OJOS",
    "PERCLOS": "OJOS",
    "BLINK_TC": "OJOS",
    "BLINK_FB": "OJOS",
    "IBI": "OJOS",
    "BLINK_AMPLITUDE": "OJOS",
    "REOPEN_SPEED": "OJOS",
    "FIXATION": "OJOS",
    # Boca
    "MAR": "BOCA",
    "YAWN_FREQ": "BOCA",
    "YAWN_DUR": "BOCA",
    # Cabeza
    "PITCH": "CABEZA",
    "ROLL": "CABEZA",
    "YAW": "CABEZA",
    "HEAD_DROP_VELOCITY": "CABEZA",
    "HEAD_RECOVERY": "CABEZA",
    "HEAD_MICRO_OSC": "CABEZA",
    # Facial (soporte)
    "FACIAL_ASYMMETRY": "FACIAL",
    "MUSCLE_TONE": "FACIAL",
    "LANDMARK_STABILITY": "FACIAL",
    # Manos (soporte)
    "FACE_TOUCH_FREQ": "MANOS",
    "FACE_TOUCH_DUR": "MANOS",
    "EYE_RUB": "MANOS",
    # Contexto (modulador)
    "ILLUMINATION": "CONTEXTO",
    "CIRCADIAN": "CONTEXTO",
    "MONOTONY": "CONTEXTO",
    "TIME_ON_TASK": "CONTEXTO",
}

PRIMARY_FAMILIES: Tuple[str, ...] = ("OJOS", "BOCA", "CABEZA")

# Parametros oculares que, activos por si solos, ya son evidencia suficiente de
# microsueno / somnolencia severa (no requieren corroboracion de otra familia).
STRONG_OCULAR_PARAMS: Tuple[str, ...] = ("PERCLOS", "EYE_CLOSED_MS", "BLINK_TC")


def family_of(param_id: str) -> str:
    return PARAM_FAMILY.get(str(param_id), "OTRO")


@dataclass
class StabilizerConfig:
    # Segundos que un nivel candidato debe sostenerse para COMPROMETER una
    # subida a ese nivel. Indexado por nivel destino (0..4).
    t_up_s: Tuple[float, float, float, float, float] = (0.0, 1.0, 1.5, 2.0, 2.5)
    # Segundos para confirmar una BAJADA de nivel (mas rapido que subir).
    t_down_s: float = 1.2
    # Ventana para considerar "recientemente activa" una familia (corroboracion).
    corroboration_window_s: float = 12.0
    # Nivel a partir del cual se exige corroboracion multi-cue.
    corroboration_min_level: int = 3
    # Nº minimo de familias primarias distintas para corroborar niveles altos.
    corroboration_min_families: int = 2
    # Tope de nivel cuando hay evidencia de una sola familia (sin microsueno).
    single_family_level_cap: int = 2

    @classmethod
    def from_env(cls, getenv) -> "StabilizerConfig":
        def f(name: str, default: float) -> float:
            try:
                return float(getenv(name, str(default)))
            except (TypeError, ValueError):
                return default

        def i(name: str, default: int) -> int:
            try:
                return int(float(getenv(name, str(default))))
            except (TypeError, ValueError):
                return default

        base = cls()
        return cls(
            t_up_s=(
                0.0,
                f("SOMNO_TUP_1_S", base.t_up_s[1]),
                f("SOMNO_TUP_2_S", base.t_up_s[2]),
                f("SOMNO_TUP_3_S", base.t_up_s[3]),
                f("SOMNO_TUP_4_S", base.t_up_s[4]),
            ),
            t_down_s=f("SOMNO_TDOWN_S", base.t_down_s),
            corroboration_window_s=f("SOMNO_CORROB_WINDOW_S", base.corroboration_window_s),
            corroboration_min_level=i("SOMNO_CORROB_MIN_LEVEL", base.corroboration_min_level),
            corroboration_min_families=i("SOMNO_CORROB_MIN_FAMILIES", base.corroboration_min_families),
            single_family_level_cap=i("SOMNO_SINGLE_FAMILY_CAP", base.single_family_level_cap),
        )


@dataclass
class LevelStabilizer:
    """Convierte el nivel crudo en un nivel comprometido, robusto a falsos positivos."""

    config: StabilizerConfig = field(default_factory=StabilizerConfig)
    committed_level: int = 0
    # Historial (ts, familia) de familias primarias vistas con evento.
    _family_hist: Deque[Tuple[float, str]] = field(default_factory=lambda: deque(maxlen=4000))
    # Momento en que el nivel candidato alcanzo/supero cada nivel de forma continua.
    _candidate_level: int = 0
    _candidate_since: float = 0.0
    _below_since: Optional[float] = None

    def reset(self) -> None:
        self.committed_level = 0
        self._family_hist.clear()
        self._candidate_level = 0
        self._candidate_since = 0.0
        self._below_since = None

    def _active_primary_families(self, ts: float) -> Set[str]:
        while self._family_hist and (ts - self._family_hist[0][0]) > self.config.corroboration_window_s:
            self._family_hist.popleft()
        return {fam for _, fam in self._family_hist if fam in PRIMARY_FAMILIES}

    def update(
        self,
        ts: float,
        raw_level: int,
        active_event_params: Iterable[str],
        forced_min_level: int = 0,
    ) -> Dict:
        """Devuelve dict con committed_level, raw_level, familias y corroboracion."""
        raw_level = max(0, min(4, int(raw_level)))
        active = {str(p) for p in active_event_params}

        # Registrar familias primarias activas este frame.
        for p in active:
            fam = family_of(p)
            if fam in PRIMARY_FAMILIES:
                self._family_hist.append((float(ts), fam))

        active_families = self._active_primary_families(ts)
        strong_ocular = any(p in active for p in STRONG_OCULAR_PARAMS)

        # 1) Corroboracion: limitar el nivel candidato si no hay evidencia suficiente.
        candidate = raw_level
        corroborated = True
        if raw_level >= self.config.corroboration_min_level:
            enough_families = len(active_families) >= self.config.corroboration_min_families
            if not (enough_families or strong_ocular):
                candidate = min(raw_level, self.config.single_family_level_cap)
                corroborated = False

        # 2) Persistencia + 3) histeresis sobre el nivel candidato.
        committed = self._apply_persistence(ts, candidate)

        # El forzado por reglas (RuleEngine) actua como piso duro: es una decision
        # deliberada por ventana larga (p.ej. fatiga sostenida 30 min), no un pico
        # de un frame, asi que se respeta por encima de la persistencia.
        forced_min_level = max(0, min(4, int(forced_min_level)))
        if forced_min_level > committed:
            committed = forced_min_level
            self.committed_level = committed

        return {
            "committed_level": committed,
            "raw_level": raw_level,
            "candidate_level": candidate,
            "corroborated": corroborated,
            "active_families": sorted(active_families),
            "strong_ocular": strong_ocular,
        }

    def _apply_persistence(self, ts: float, candidate: int) -> int:
        # Actualizar cuanto lleva el candidato en su nivel actual.
        if candidate != self._candidate_level:
            self._candidate_level = candidate
            self._candidate_since = ts
        held = ts - self._candidate_since

        committed = self.committed_level

        if candidate > committed:
            # Subida: exigir sostener T_up del nivel destino inmediato superior.
            target = committed + 1
            if candidate >= target and held >= self.config.t_up_s[max(0, min(4, target))]:
                committed = target
                # Permitir escalones consecutivos en frames siguientes (no saltar).
            self._below_since = None
        elif candidate < committed:
            # Bajada: confirmar tras T_down.
            if self._below_since is None:
                self._below_since = ts
            if ts - self._below_since >= self.config.t_down_s:
                committed = max(candidate, committed - 1)
                self._below_since = ts if committed > candidate else None
        else:
            self._below_since = None

        self.committed_level = max(0, min(4, committed))
        return self.committed_level


if __name__ == "__main__":
    # Demostracion rapida: un pico aislado de nivel 4 NO debe comprometer nivel 4.
    st = LevelStabilizer()
    print("pico aislado:")
    print(st.update(0.0, 4, ["PERCLOS"]))  # candidato 4 (microsueno) pero sin persistencia
    print(st.update(0.05, 0, []))          # vuelve a 0 enseguida -> committed sigue bajo
    # Evidencia sostenida y corroborada:
    st.reset()
    print("evidencia sostenida corroborada:")
    out = None
    t = 0.0
    while t < 6.0:
        out = st.update(t, 3, ["PERCLOS", "PITCH"])  # ojos + cabeza
        t += 0.1
    print(out)
