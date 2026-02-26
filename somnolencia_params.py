from dataclasses import dataclass
from pathlib import Path
import re


@dataclass
class SomnolenciaParams:
    ear_threshold: float = 0.21
    consec_frames: int = 15
    mar_yawn_threshold: float = 0.35
    head_pitch_threshold: float = 20.0
    head_pitch_reset_threshold: float = 15.0
    eye_rub_distance_ratio: float = 0.22
    eye_rub_min_frames: int = 4
    eye_rub_ear_max: float = 0.30
    alarm_message: str = "wake up"
    alarm_repeat_seconds: float = 1.2
    buzzer_gpio_pin: int = 17
    buzzer_active_high: bool = True
    buzzer_enabled: bool = True
    buzzer_beep_on_seconds: float = 0.35
    buzzer_beep_off_seconds: float = 0.35
    perclos_window_seconds: float = 60.0
    blink_open_threshold: float = 0.24
    blink_min_duration_s: float = 0.06
    blink_max_duration_s: float = 0.8
    fixation_motion_threshold_px_s: float = 28.0
    head_drop_velocity_threshold_dps: float = 18.0
    head_recovery_start_pitch: float = 20.0
    head_recovery_reset_pitch: float = 12.0
    head_micro_window_seconds: float = 6.0
    face_touch_distance_ratio: float = 0.32
    face_touch_window_seconds: float = 60.0
    facial_tone_alpha: float = 0.04
    facial_stability_scale: float = 35.0
    context_monotony_window_seconds: float = 60.0
    illumination_dark_threshold: float = 0.28
    illumination_bright_threshold: float = 0.72
    emg_loc_eye_closed_seconds: float = 2.5
    emg_convulsive_window_seconds: float = 8.0
    emg_convulsive_min_hz: float = 2.0
    emg_convulsive_max_hz: float = 8.0
    emg_convulsive_power_ratio: float = 0.45
    emg_stroke_asymmetry_threshold: float = 0.09
    emg_stroke_sustain_seconds: float = 1.5
    emg_lateral_roll_threshold: float = 45.0
    emg_lateral_sustain_seconds: float = 1.8
    emg_face_out_seconds: float = 2.0
    emg_face_out_yaw_justification: float = 30.0
    emg_absence_no_blink_seconds: float = 8.0
    emg_absence_fixation_seconds: float = 4.0


def _extract_first(patterns, content):
    for pattern in patterns:
        match = re.search(pattern, content, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def load_somnolencia_params(txt_path="CODIGO_IMPORTANTE_RASPBERRY_PI.txt"):
    params = SomnolenciaParams()
    path = Path(txt_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / txt_path
    if not path.exists():
        return params

    content = path.read_text(encoding="utf-8", errors="ignore")
    ear_raw = _extract_first(
        [
            r"EAR_THRESHOLD\s*=\s*([0-9]*\.?[0-9]+)",
            r"ear_value\s*<\s*([0-9]*\.?[0-9]+)",
        ],
        content,
    )
    consec_raw = _extract_first([r"CONSEC_FRAMES\s*=\s*([0-9]+)"], content)
    mar_raw = _extract_first([r"mar_value\s*>=\s*([0-9]*\.?[0-9]+)"], content)
    pitch_raw = _extract_first(
        [
            r"pitch\s*>\s*([0-9]*\.?[0-9]+)",
            r"head_pitch_threshold\s*=\s*([0-9]*\.?[0-9]+)",
        ],
        content,
    )
    pitch_reset_raw = _extract_first(
        [
            r"pitch\s*<\s*([0-9]*\.?[0-9]+)",
            r"head_pitch_reset_threshold\s*=\s*([0-9]*\.?[0-9]+)",
        ],
        content,
    )
    rub_ratio_raw = _extract_first([r"eye_rub_distance_ratio\s*=\s*([0-9]*\.?[0-9]+)"], content)
    rub_frames_raw = _extract_first([r"eye_rub_min_frames\s*=\s*([0-9]+)"], content)
    rub_ear_raw = _extract_first([r"eye_rub_ear_max\s*=\s*([0-9]*\.?[0-9]+)"], content)
    buzzer_pin_raw = _extract_first(
        [
            r"BUZZER_GPIO_PIN\s*=\s*([0-9]+)",
            r"buzzer(?:_pin)?\s*[:=]\s*GPIO\s*([0-9]+)",
            r"buzzer[^\\n\\r]{0,80}GPIO\s*([0-9]+)",
        ],
        content,
    )
    buzzer_active_high_raw = _extract_first(
        [
            r"BUZZER_ACTIVE_HIGH\s*=\s*(True|False|1|0)",
            r"buzzer_active_high\s*=\s*(True|False|1|0)",
        ],
        content,
    )
    buzzer_enabled_raw = _extract_first(
        [
            r"BUZZER_ENABLED\s*=\s*(True|False|1|0)",
            r"buzzer_enabled\s*=\s*(True|False|1|0)",
        ],
        content,
    )
    buzzer_on_raw = _extract_first(
        [
            r"BUZZER_BEEP_ON_SECONDS\s*=\s*([0-9]*\.?[0-9]+)",
            r"buzzer_beep_on_seconds\s*=\s*([0-9]*\.?[0-9]+)",
        ],
        content,
    )
    buzzer_off_raw = _extract_first(
        [
            r"BUZZER_BEEP_OFF_SECONDS\s*=\s*([0-9]*\.?[0-9]+)",
            r"buzzer_beep_off_seconds\s*=\s*([0-9]*\.?[0-9]+)",
        ],
        content,
    )

    if ear_raw is not None:
        params.ear_threshold = float(ear_raw)
    if consec_raw is not None:
        params.consec_frames = int(consec_raw)
    if mar_raw is not None:
        params.mar_yawn_threshold = float(mar_raw)
    if pitch_raw is not None:
        params.head_pitch_threshold = float(pitch_raw)
    if pitch_reset_raw is not None:
        params.head_pitch_reset_threshold = float(pitch_reset_raw)
    if rub_ratio_raw is not None:
        params.eye_rub_distance_ratio = float(rub_ratio_raw)
    if rub_frames_raw is not None:
        params.eye_rub_min_frames = int(rub_frames_raw)
    if rub_ear_raw is not None:
        params.eye_rub_ear_max = float(rub_ear_raw)
    if buzzer_pin_raw is not None:
        params.buzzer_gpio_pin = int(buzzer_pin_raw)
    if buzzer_active_high_raw is not None:
        params.buzzer_active_high = buzzer_active_high_raw.lower() in ("true", "1")
    if buzzer_enabled_raw is not None:
        params.buzzer_enabled = buzzer_enabled_raw.lower() in ("true", "1")
    if buzzer_on_raw is not None:
        params.buzzer_beep_on_seconds = float(buzzer_on_raw)
    if buzzer_off_raw is not None:
        params.buzzer_beep_off_seconds = float(buzzer_off_raw)
    return params
