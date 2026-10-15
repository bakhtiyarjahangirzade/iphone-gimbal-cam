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
    """Converts the target's position error into gimbal velocities.

    speed       maximum gimbal speed (0..1 of joystick range)
    response    proportional gain: how hard it reacts to a given error
    smoothing   0 = instant acceleration, 0.9 = very soft start
    sharp_stop  decelerate/stop instantly (no smoothing tail, no overshoot)
    lead        look-ahead in seconds using the target's own motion (helps with fast movers)
    """

    def __init__(self):
        self.target_x = 0.5
        self.target_y = 0.42
        self.deadzone = 0.04
        self.kp = 3.0
        self.max_speed = 1.0
        self.smoothing = 0.3
        self.sharp_stop = True
        self.lead = 0.12
        self.invert_pan = False
        self.invert_tilt = False
        self._prev_err = None
        self._prev_t = 0.0
        self._vel_est = (0.0, 0.0)
        self._out = (0.0, 0.0)
        self._int = (0.0, 0.0)
        self._lost_since = None

    def reset(self):
        self._prev_err = None
        self._vel_est = (0.0, 0.0)
        self._out = (0.0, 0.0)
        self._int = (0.0, 0.0)

    def _axis(self, err, prev_out):
        dz = self.deadzone
        if abs(err) < dz:
            raw = 0.0
        else:
            e = err - dz * (1 if err > 0 else -1)
            raw = max(-self.max_speed, min(self.max_speed, self.kp * e))
        decelerating = abs(raw) < abs(prev_out) or raw * prev_out < 0
        if self.sharp_stop and decelerating:
            return raw                               # stop / slow down immediately
        a = 1.0 - max(0.0, min(0.95, self.smoothing))
        return prev_out + a * (raw - prev_out)        # soft acceleration

    def update(self, point):
        now = time.monotonic()
        if point is None:
            if self._lost_since is None:
                self._lost_since = now
            if self.sharp_stop or now - self._lost_since > 0.3:
                self._out = (0.0, 0.0)
                self._int = (0.0, 0.0)
                self._prev_err = None
            return self._out
        self._lost_since = None

        ex = point[0] - self.target_x
        ey = point[1] - self.target_y
        if self._prev_err is not None:
            dt = max(1e-3, now - self._prev_t)
            vx = (ex - self._prev_err[0]) / dt
            vy = (ey - self._prev_err[1]) / dt
            self._vel_est = (0.6 * self._vel_est[0] + 0.4 * vx, 0.6 * self._vel_est[1] + 0.4 * vy)
        self._prev_err = (ex, ey)
        self._prev_t = now

        # look ahead: where the target will be shortly
        px = ex + self._vel_est[0] * self.lead
        py = ey + self._vel_est[1] * self.lead

        ip = self._axis(px, self._int[0])   # internal state is in "error space"
        it = self._axis(py, self._int[1])
        self._int = (ip, it)
        pan = -ip if self.invert_pan else ip
        tilt = it if self.invert_tilt else -it  # subject above centre (ey<0) -> tilt up (+)
        self._out = (pan, tilt)
        return self._out
