"""Face / body tracking with MediaPipe and a PD controller that drives the gimbal."""
import time

import numpy as np


class Tracker:
    MODES = ("off", "face", "body")

    def __init__(self):
        self.mode = "off"
        self._face = None
        self._pose = None
        self.last_box = None  # (x0, y0, x1, y1) normalized, for drawing

    def _ensure(self):
        import mediapipe as mp
        if self.mode == "face" and self._face is None:
            self._face = mp.solutions.face_detection.FaceDetection(
                model_selection=1, min_detection_confidence=0.5)
        if self.mode == "body" and self._pose is None:
            self._pose = mp.solutions.pose.Pose(
                model_complexity=0, smooth_landmarks=True,
                min_detection_confidence=0.5, min_tracking_confidence=0.5)

    def process(self, rgb: np.ndarray):
        """rgb: small HxWx3 uint8 frame. Returns target point (x, y) normalized, or None."""
        if self.mode == "off":
            self.last_box = None
            return None
        self._ensure()
        if self.mode == "face":
            res = self._face.process(rgb)
            if not res.detections:
                self.last_box = None
                return None
            best = max(res.detections, key=lambda d: d.location_data.relative_bounding_box.width
                       * d.location_data.relative_bounding_box.height)
            b = best.location_data.relative_bounding_box
            self.last_box = (b.xmin, b.ymin, b.xmin + b.width, b.ymin + b.height)
            return (b.xmin + b.width / 2, b.ymin + b.height / 2)

        res = self._pose.process(rgb)
        if not res.pose_landmarks:
            self.last_box = None
            return None
        lm = res.pose_landmarks.landmark
        pts = [(p.x, p.y) for i, p in enumerate(lm) if i in (0, 11, 12, 23, 24) and p.visibility > 0.5]
        if not pts:
            self.last_box = None
            return None
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        self.last_box = (min(xs), min(ys), max(xs), max(ys))
        # aim at upper body: midpoint between nose and shoulders
        return (sum(xs) / len(xs), min(ys) + (max(ys) - min(ys)) * 0.35)


class FollowController:
    """Converts target position error into gimbal velocities."""

    def __init__(self):
        self.target_x = 0.5
        self.target_y = 0.42
        self.deadzone = 0.04
        self.kp = 2.2
        self.kd = 0.25
        self.max_speed = 0.6
        self.invert_pan = False
        self.invert_tilt = False
        self._prev = None
        self._prev_t = 0.0
        self._out = (0.0, 0.0)
        self._lost_since = None

    def update(self, point):
        now = time.monotonic()
        if point is None:
            if self._lost_since is None:
                self._lost_since = now
            if now - self._lost_since > 0.3:
                self._out = (0.0, 0.0)
            return self._out
        self._lost_since = None

        ex = point[0] - self.target_x
        ey = point[1] - self.target_y
        dt = max(1e-3, now - self._prev_t) if self._prev else None
        dx = (ex - self._prev[0]) / dt if dt else 0.0
        dy = (ey - self._prev[1]) / dt if dt else 0.0
        self._prev = (ex, ey)
        self._prev_t = now

        def axis(e, d):
            if abs(e) < self.deadzone:
                return 0.0
            e = e - self.deadzone * (1 if e > 0 else -1)
            v = self.kp * e + self.kd * d * 0.05
            return max(-self.max_speed, min(self.max_speed, v))

        pan = axis(ex, dx)
        tilt = -axis(ey, dy)  # subject above centre -> tilt up
        if self.invert_pan:
            pan = -pan
        if self.invert_tilt:
            tilt = -tilt
        # light smoothing
        a = 0.5
        self._out = (a * pan + (1 - a) * self._out[0], a * tilt + (1 - a) * self._out[1])
        return self._out
