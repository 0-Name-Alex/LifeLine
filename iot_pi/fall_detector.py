"""Rule-based fall / man-down detector for the Raspberry Pi (no training data needed).

Why rules and not the Random Forest: the existing training CSV has NO fall labels, so no
fall model can be trained from it. This detector looks for two things:

  1. fall_suspected : a big impact (>= IMPACT_G) followed, within CONFIRM_S seconds, by the
                      torso lying over (tilt >= TILT_DEG from the worker's upright posture)
                      and staying still.
  2. man_down       : torso tilted over AND still for >= MAN_DOWN_S seconds, with or
                      without a detected impact. This is the part that still works at 1 Hz,
                      because it checks the *aftermath* of a fall, which lasts many seconds.

At 1 reading/second the impact itself (~50-200 ms) is usually missed. If the ESP32 adds
`accel_peak_g`, `accel_min_g` (max/min over the last second, sampled at >=50 Hz) or its own
`fall` flag to the JSON (see esp32/), they are used automatically.

The upright direction is FIXED (UPRIGHT below), no calibration step.

Units: accel_x/y/z in m/s^2 (Adafruit_MPU6050 getEvent), *_g fields in g.
"""
from __future__ import annotations
import math
from collections import deque

G = 9.80665

IMPACT_G = 2.5          # |a| peak treated as an impact
FREEFALL_G = 0.5        # |a| dip treated as free fall (only visible with high-rate peak/min fields)
TILT_DEG = 60.0         # torso angle from upright that counts as "lying"
RECOVER_TILT_DEG = 40.0 # back below this + moving = recovered
CONFIRM_S = 10.0        # after an impact, how long to wait for lying + still
MAN_DOWN_S = 20.0       # lying + still this long = man down, even without impact
STILL_DELTA = 0.8       # m/s^2: max change between consecutive readings to call it "still"
# Gravity direction when the worker stands: MPU6050 z axis pointing UP (accel_z ~ +9.8).
# Fixed, no calibration. Change it (or pass --upright) if the sensor is mounted differently.
UPRIGHT = (0.0, 0.0, 1.0)
STUCK_SAMPLES = 3       # identical x/y/z this many times in a row = frozen sensor

STATE_PRIORITY = {'unknown': -1, 'ok': 0, 'recovered': 0, 'impact': 1, 'fall_suspected': 2, 'man_down': 3}


def _unit(v):
    n = math.sqrt(sum(c * c for c in v))
    return tuple(c / n for c in v) if n > 0 else None


def _angle_deg(u, v):
    dot = max(-1.0, min(1.0, sum(a * b for a, b in zip(u, v))))
    return math.degrees(math.acos(dot))


def _num(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


class _WorkerState:
    def __init__(self):
        self.prev = None                # previous accel vector
        self.last_t = None
        self.recent = deque(maxlen=STUCK_SAMPLES)
        self.still_since = None
        self.impact_t = None
        self.state = 'ok'
        self.state_since = None


class FallDetector:
    def __init__(self, upright=None, reset_gap_s=60.0):
        """upright: gravity direction (x, y, z) when the worker stands, e.g. (0, -1, 0) if
        accel_y reads -9.8 when standing. Default UPRIGHT = z axis up."""
        self.upright = _unit(upright or UPRIGHT)
        self.reset_gap_s = reset_gap_s
        self.workers = {}

    def update(self, worker, t, packet):
        """t: seconds (any monotonic clock). Returns a dict describing the fall state."""
        st = self.workers.get(worker)
        if st is None or (st.last_t is not None and t - st.last_t > self.reset_gap_s):
            st = self.workers[worker] = _WorkerState()
        out = {'fall_state': st.state, 'fall_reason': '', 'tilt_deg': None,
               'accel_g': None, 'still_s': 0.0}

        a = tuple(_num(packet.get(k)) for k in ('accel_x', 'accel_y', 'accel_z'))
        if any(c is None for c in a) or max(abs(c) for c in a) > 16 * G:
            out.update(fall_state='unknown', fall_reason='accel_missing_or_out_of_range')
            return out
        st.recent.append(a)
        if len(st.recent) == STUCK_SAMPLES and len(set(st.recent)) == 1:
            out.update(fall_state='unknown', fall_reason='accel_stuck_check_mpu6050_wiring')
            return out
        st.last_t = t

        mag = math.sqrt(sum(c * c for c in a))
        g_now = mag / G
        out['accel_g'] = round(g_now, 3)
        peak_g = max(g_now, _num(packet.get('accel_peak_g')) or 0.0)
        min_g = min(g_now, _num(packet.get('accel_min_g')) or g_now)
        u = _unit(a)

        delta = math.sqrt(sum((c - p) ** 2 for c, p in zip(a, st.prev))) if st.prev else 0.0
        st.prev = a
        still = delta < STILL_DELTA and abs(g_now - 1.0) < 0.2 and peak_g < 1.5
        if not still or st.still_since is None:
            st.still_since = t if still else None
        still_s = (t - st.still_since) if st.still_since is not None else 0.0
        out['still_s'] = round(still_s, 1)

        tilt = _angle_deg(u, self.upright)
        out['tilt_deg'] = round(tilt, 1)

        esp_fall = str(packet.get('fall', '')).strip().lower() in ('1', 'true')
        impact = peak_g >= IMPACT_G
        lying = tilt >= TILT_DEG
        new_state, reason = st.state, ''

        if esp_fall:
            st.impact_t = t
            if STATE_PRIORITY[st.state] < STATE_PRIORITY['fall_suspected']:
                new_state = 'fall_suspected'
            reason = 'esp32_fall_flag'
        elif impact:
            st.impact_t = t
            if STATE_PRIORITY[st.state] < STATE_PRIORITY['impact']:
                new_state = 'impact'
            reason = f'peak {peak_g:.1f} g' + (f', free-fall dip {min_g:.2f} g' if min_g <= FREEFALL_G else '')

        if st.impact_t is not None and t - st.impact_t <= CONFIRM_S and lying and still_s >= 2.0 \
                and STATE_PRIORITY[new_state] < STATE_PRIORITY['fall_suspected']:
            new_state, reason = 'fall_suspected', 'impact then lying still'
        if lying and still_s >= MAN_DOWN_S:
            new_state, reason = 'man_down', f'lying still {still_s:.0f} s'

        if st.impact_t is not None and t - st.impact_t > CONFIRM_S and new_state == 'impact':
            new_state, reason = 'ok', 'impact without fall posture'
            st.impact_t = None
        if STATE_PRIORITY[new_state] >= 2 and not esp_fall and tilt < RECOVER_TILT_DEG and not still:
            new_state, reason = 'recovered', 'upright and moving again'
            st.impact_t = None
        elif new_state == 'recovered' and still_s == 0:
            new_state = 'ok'

        if new_state != st.state:
            st.state, st.state_since = new_state, t
        out.update(fall_state=st.state, fall_reason=reason)
        return out
