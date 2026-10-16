"""Hand-gesture control with MediaPipe Hands.

  open palm held 1 s  -> "palm"   (toggle face tracking)
  fist held 1 s       -> "fist"   (stop everything)
Each gesture fires once per hold; the hand has to change pose to fire again.
"""
import time

HOLD_SECONDS = 1.0
FINGERS = ((8, 6), (12, 10), (16, 14), (20, 18))   # (tip, pip) for index..pinky


class GestureDetector:
    def __init__(self):
        self._hands = None
        self._pose = None
        self._since = 0.0
        self._fired = False

    def _ensure(self):
        if self._hands is None:
            import mediapipe as mp
            self._hands = mp.solutions.hands.Hands(
                static_image_mode=False, max_num_hands=1, model_complexity=0,
                min_detection_confidence=0.6, min_tracking_confidence=0.5)

    @staticmethod
    def classify(lm):
        """lm: 21 landmarks with .x/.y (normalized, y down). Returns 'palm', 'fist' or None."""
        wrist = lm[0]
        extended = [lm[t].y < lm[p].y - 0.02 and lm[t].y < wrist.y for t, p in FINGERS]
        folded = [lm[t].y > lm[p].y for t, p in FINGERS]
        if all(extended):
            return "palm"
        if all(folded):
            return "fist"
        return None

    def process(self, rgb):
        self._ensure()
        res = self._hands.process(rgb)
        pose = None
        if res.multi_hand_landmarks:
            pose = self.classify(res.multi_hand_landmarks[0].landmark)
        now = time.monotonic()
        if pose != self._pose:
            self._pose, self._since, self._fired = pose, now, False
            return None
        if pose and not self._fired and now - self._since >= HOLD_SECONDS:
            self._fired = True
            return pose
        return None
