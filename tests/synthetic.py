"""Synthetic flight tracks for tests."""

import math

import numpy as np


class Track:
    """Integrates simple flight phases at 1 s resolution."""

    def __init__(self, t0=12 * 3600.0, x=0.0, y=0.0, z=1.0, heading=90.0, wind=(0.0, 0.0)):
        self.t, self.x, self.y, self.z = [t0], [x], [y], [z]
        self.heading = heading
        self.wind = wind

    def _step(self, vx, vy, vz, drift=True):
        wx, wy = self.wind if drift else (0.0, 0.0)
        self.t.append(self.t[-1] + 1.0)
        self.x.append(self.x[-1] + vx + wx)
        self.y.append(self.y[-1] + vy + wy)
        self.z.append(self.z[-1] + vz)

    def ground(self, seconds):
        for _ in range(int(seconds)):
            self._step(0, 0, 0, drift=False)
        return self

    def straight(self, seconds, speed, vz, heading=None, drift=True):
        if heading is not None:
            self.heading = heading
        h = math.radians(self.heading)
        for _ in range(int(seconds)):
            self._step(speed * math.sin(h), speed * math.cos(h), vz, drift)
        return self

    def accelerate(self, seconds, v_end, vz=0.0):
        h = math.radians(self.heading)
        for k in range(int(seconds)):
            v = v_end * (k + 1) / seconds
            self._step(v * math.sin(h), v * math.cos(h), vz, drift=False)
        return self

    def circle(self, seconds, speed=25.0, radius=60.0, vz=1.0, direction=1):
        rate = math.degrees(speed / radius) * direction
        for _ in range(int(seconds)):
            self.heading = (self.heading + rate) % 360
            h = math.radians(self.heading)
            self._step(speed * math.sin(h), speed * math.cos(h), vz)
        return self

    def s_turns(self, seconds, speed=25.0, amplitude_m=10.0):
        for k in range(int(seconds)):
            self.heading = (self.heading + (20 if (k // 6) % 2 == 0 else -20)) % 360
            h = math.radians(self.heading)
            vz = amplitude_m * math.cos(k / 6 * math.pi) / 6
            self._step(speed * math.sin(h), speed * math.cos(h), vz)
        return self

    def land(self, seconds_on_ground=60):
        self.z[-1] = self.z[0]
        return self.ground(seconds_on_ground)

    def arrays(self):
        return (np.array(self.t), np.array(self.x), np.array(self.y), np.array(self.z))


def winch_launch(track: Track, release_agl=300.0):
    """Ground roll + winch climb at 12 m/s to release_agl."""
    track.ground(60).accelerate(5, 20.0)
    climb_s = int(release_agl / 12)
    track.straight(climb_s, 20.0, 12.0, drift=False)
    track.straight(3, 28.0, 0.0, drift=False)   # push-over at release
    return track
