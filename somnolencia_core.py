import cv2
import numpy as np
from scipy.spatial import distance as dist


OJO_IZQ = [362, 385, 387, 263, 373, 380]
OJO_DER = [33, 160, 158, 133, 153, 144]

# Landmarks de boca para MAR
BOCA = [61, 291, 0, 17, 84, 314, 405, 321]

# Landmarks usados para estimar pose de cabeza (pitch/yaw/roll)
POSE_IDX = [1, 152, 33, 263, 61, 291]


def _clamp01(v):
    return max(0.0, min(1.0, float(v)))


def _eye_coords(landmarks, points, w, h, rotation_index=0):
    coords = []
    for i in points:
        lm = landmarks[i]
        x = lm.x * w
        y = lm.y * h

        # Ajustar coordenadas según rotación para que sean equivalentes al sistema original
        if rotation_index == 1:  # 90 grados horario
            x, y = y, w - x
        elif rotation_index == 2:  # 180 grados
            x, y = w - x, h - y
        elif rotation_index == 3:  # 90 grados antihorario (270 grados horario)
            x, y = h - y, x

        coords.append(np.array([x, y]))
    return coords


def eye_metrics(landmarks, points, w, h, rotation_index=0):
    """Devuelve (EAR, ancho_horizontal_px) de un ojo.

    El ancho horizontal p1-p4 (comisura a comisura) es la referencia que menos
    se escorza de frente y la que MAS se acorta cuando el ojo se aleja de la
    camara por giro (yaw). Sirve para elegir el ojo mas frontal (fiable).
    """
    c = _eye_coords(landmarks, points, w, h, rotation_index)
    p2_p6 = dist.euclidean(c[1], c[5])
    p3_p5 = dist.euclidean(c[2], c[4])
    p1_p4 = dist.euclidean(c[0], c[3])
    ear = (p2_p6 + p3_p5) / (2.0 * p1_p4) if p1_p4 > 0 else 0.0
    return ear, float(p1_p4)


def fuse_ear(ear_left, w_left, ear_right, w_right, yaw_delta_deg,
             yaw_soft=12.0, yaw_hard=30.0):
    """Combina el EAR de ambos ojos segun el giro de la cabeza (yaw).

    - De frente (|yaw| <= yaw_soft): promedio de ambos ojos (ambos fiables).
    - En giro: se pondera progresivamente hacia el ojo mas FRONTAL, identificado
      por el mayor ancho horizontal proyectado (el que menos se escorza). El ojo
      que se aleja de la camara pierde fiabilidad (auto-oclusion por la nariz,
      landmarks ruidosos) y se le baja el peso hasta 0 en |yaw| >= yaw_hard.
    """
    ay = abs(float(yaw_delta_deg))
    if ay <= yaw_soft:
        return 0.5 * (ear_left + ear_right)
    if w_left >= w_right:
        near, far = float(ear_left), float(ear_right)
    else:
        near, far = float(ear_right), float(ear_left)
    t = _clamp01((ay - yaw_soft) / max(1e-6, yaw_hard - yaw_soft))
    w_near = 0.5 + 0.5 * t
    return w_near * near + (1.0 - w_near) * far


def get_ear(landmarks, points, w, h, rotation_index=0):
    return eye_metrics(landmarks, points, w, h, rotation_index)[0]


def get_mar(landmarks, points, w, h, rotation_index=0):
    coords = []
    for i in points:
        lm = landmarks[i]
        x = lm.x * w
        y = lm.y * h
        
        # Ajustar coordenadas según rotación para que sean equivalentes al sistema original
        if rotation_index == 1:  # 90 grados horario
            x, y = y, w - x
        elif rotation_index == 2:  # 180 grados
            x, y = w - x, h - y
        elif rotation_index == 3:  # 90 grados antihorario (270 grados horario)
            x, y = h - y, x
            
        coords.append(np.array([x, y]))

    # MAR = (A + B + C) / (2*D)
    a = dist.euclidean(coords[1], coords[7])
    b = dist.euclidean(coords[2], coords[6])
    c = dist.euclidean(coords[3], coords[5])
    d = dist.euclidean(coords[0], coords[4])
    return (a + b + c) / (2.0 * d) if d > 0 else 0.0


def _rotation_to_euler_deg(rot_matrix):
    sy = np.sqrt(rot_matrix[0, 0] ** 2 + rot_matrix[1, 0] ** 2)
    singular = sy < 1e-6

    if not singular:
        x = np.arctan2(rot_matrix[2, 1], rot_matrix[2, 2])
        y = np.arctan2(-rot_matrix[2, 0], sy)
        z = np.arctan2(rot_matrix[1, 0], rot_matrix[0, 0])
    else:
        x = np.arctan2(-rot_matrix[1, 2], rot_matrix[1, 1])
        y = np.arctan2(-rot_matrix[2, 0], sy)
        z = 0.0

    return float(np.degrees(x)), float(np.degrees(y)), float(np.degrees(z))


def get_head_pose(landmarks, w, h, rotation_index=0):
    # Modelo 3D estandarizado de rostro (6 puntos)
    model_points = np.array(
        [
            (0.0, 0.0, 0.0),
            (0.0, -330.0, -65.0),
            (-225.0, 170.0, -135.0),
            (225.0, 170.0, -135.0),
            (-150.0, -150.0, -125.0),
            (150.0, -150.0, -125.0),
        ],
        dtype=np.float64,
    )

    image_points = []
    for i in POSE_IDX:
        lm = landmarks[i]
        x = lm.x * w
        y = lm.y * h
        
        # Ajustar coordenadas según rotación para que sean equivalentes al sistema original
        if rotation_index == 1:  # 90 grados horario
            x, y = y, w - x
        elif rotation_index == 2:  # 180 grados
            x, y = w - x, h - y
        elif rotation_index == 3:  # 90 grados antihorario (270 grados horario)
            x, y = h - y, x
            
        image_points.append([x, y])
    
    image_points = np.array(image_points, dtype=np.float64)

    camera_matrix = np.array(
        [[w, 0, w / 2.0], [0, w, h / 2.0], [0, 0, 1]],
        dtype=np.float64,
    )
    dist_coeffs = np.zeros((4, 1), dtype=np.float64)

    ok, rvec, _ = cv2.solvePnP(
        model_points,
        image_points,
        camera_matrix,
        dist_coeffs,
        flags=cv2.SOLVEPNP_ITERATIVE,
    )
    if not ok:
        return 0.0, 0.0, 0.0

    rot_matrix, _ = cv2.Rodrigues(rvec)
    return _rotation_to_euler_deg(rot_matrix)
