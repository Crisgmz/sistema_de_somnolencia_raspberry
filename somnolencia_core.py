import cv2
import numpy as np
from scipy.spatial import distance as dist


OJO_IZQ = [362, 385, 387, 263, 373, 380]
OJO_DER = [33, 160, 158, 133, 153, 144]

# Landmarks de boca para MAR
BOCA = [61, 291, 0, 17, 84, 314, 405, 321]

# Landmarks usados para estimar pose de cabeza (pitch/yaw/roll)
POSE_IDX = [1, 152, 33, 263, 61, 291]


def get_ear(landmarks, points, w, h):
    coords = []
    for i in points:
        lm = landmarks[i]
        coords.append(np.array([lm.x * w, lm.y * h]))
    p2_p6 = dist.euclidean(coords[1], coords[5])
    p3_p5 = dist.euclidean(coords[2], coords[4])
    p1_p4 = dist.euclidean(coords[0], coords[3])
    return (p2_p6 + p3_p5) / (2.0 * p1_p4) if p1_p4 > 0 else 0.0


def get_mar(landmarks, points, w, h):
    coords = []
    for i in points:
        lm = landmarks[i]
        coords.append(np.array([lm.x * w, lm.y * h]))

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


def get_head_pose(landmarks, w, h):
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

    image_points = np.array(
        [[landmarks[i].x * w, landmarks[i].y * h] for i in POSE_IDX],
        dtype=np.float64,
    )

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
