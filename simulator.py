"""
Hardware-free simulator for SunBlockCore-LL.

Generates realistic solar readings using hourly baselines derived from
1.28 M rows of real deployment data (2025-05-11 → 2025-05-29,
Milieux Institute roof array, Montreal).

Drop-in replacement for hardware.parse_data() when SIM_MODE=true.
"""

import random
from datetime import datetime


class _SimState:
    """
    Each tick:
      1. Linearly interpolates between adjacent hourly anchors.
      2. Adds per-channel Gaussian noise.
      3. Exponentially smooths toward the noisy target (α=0.35) to
         avoid visible step-changes at the 1-second polling rate.

    Tuple layout per hour:
        (pv_v, pv_a, pv_w, batt_v, batt_soc,
         batt_charge_w, batt_overall_a, load_w, cpu_w)
    """

    HOURLY = {
        0:  ( 1.77, 0.110,  1.59, 12.958, 68.3,  1.59, -0.719, 10.88, 3.92),
        1:  ( 1.78, 0.128,  1.83, 12.936, 67.5,  1.83, -0.590,  9.45, 2.86),
        2:  ( 1.87, 0.096,  1.41, 13.005, 70.0,  1.41, -0.638,  9.71, 2.96),
        3:  ( 1.87, 0.095,  1.39, 13.014, 70.4,  1.39, -0.542,  8.39, 2.17),
        4:  ( 2.83, 0.094,  1.38, 13.006, 70.1,  1.38, -0.532,  8.35, 2.07),
        5:  (12.30, 0.101,  1.48, 13.000, 69.9,  1.48, -0.526,  8.21, 2.00),
        6:  (14.08, 0.230,  3.37, 13.001, 69.9,  3.37, -0.388,  8.39, 2.13),
        7:  (14.91, 0.829, 12.95, 13.048, 71.7, 12.95,  0.330,  8.63, 2.37),
        8:  (15.21, 1.240, 19.80, 13.100, 73.6, 19.80,  0.837,  8.62, 2.52),
        9:  (14.91, 1.537, 23.65, 13.127, 74.6, 23.65,  1.105,  8.93, 2.78),
        10: (14.79, 1.844, 27.88, 13.155, 75.7, 27.88,  1.408,  9.04, 2.93),
        11: (14.67, 1.889, 28.30, 13.169, 76.2, 28.30,  1.425,  9.19, 2.96),
        12: (14.72, 2.002, 29.78, 13.186, 76.8, 29.77,  1.568,  8.79, 2.60),
        13: (14.75, 1.976, 29.37, 13.223, 78.2, 29.36,  1.540,  8.78, 2.54),
        14: (14.68, 0.796, 11.87, 13.177, 76.4, 11.86,  0.219,  8.98, 2.76),  # real afternoon dip
        15: (14.42, 0.542,  7.79, 13.155, 75.6,  7.78, -0.099,  9.15, 2.91),
        16: (14.39, 0.422,  6.09, 13.134, 74.9,  6.09, -0.268,  9.64, 3.23),
        17: (14.40, 0.380,  5.50, 13.116, 74.2,  5.50, -0.347, 10.09, 3.50),
        18: (14.28, 0.250,  3.61, 13.084, 73.0,  3.60, -0.482,  9.96, 3.38),
        19: (13.80, 0.173,  2.49, 13.066, 72.3,  2.49, -0.495,  8.99, 2.68),
        20: ( 9.42, 0.149,  2.10, 13.053, 71.8,  2.10, -0.571,  9.55, 3.03),
        21: ( 1.88, 0.157,  2.22, 13.034, 71.1,  2.22, -0.633, 10.47, 3.61),
        22: ( 1.73, 0.146,  2.06, 13.016, 70.4,  2.06, -0.645, 10.44, 3.62),
        23: ( 1.73, 0.127,  1.82, 12.987, 69.4,  1.82, -0.781, 11.93, 4.45),
    }

    # Gaussian noise σ per channel
    SIGMA = (0.20, 0.06, 0.9, 0.015, 0.4, 0.8, 0.12, 1.3, 0.8)
    ALPHA = 0.35  # exponential smoothing factor

    def __init__(self):
        self._prev: list | None = None

    @staticmethod
    def _interp(hour: int, frac: float) -> tuple:
        r0 = _SimState.HOURLY[hour]
        r1 = _SimState.HOURLY[(hour + 1) % 24]
        return tuple(a + (b - a) * frac for a, b in zip(r0, r1))

    def step(self) -> dict:
        now  = datetime.now()
        frac = (now.minute * 60 + now.second) / 3600.0
        base = list(self._interp(now.hour, frac))

        noisy = [b + random.gauss(0, s) for b, s in zip(base, self.SIGMA)]
        if self._prev is not None:
            noisy = [p + self.ALPHA * (n - p) for p, n in zip(self._prev, noisy)]
        self._prev = noisy

        pv_v, pv_a, pv_w, bv, soc, bcp, boc, load, cpu = noisy
        return {
            "Timestamp":          now.strftime("%Y-%m-%d %H:%M:%S"),
            "PVVoltage":          round(max(0.0, pv_v), 2),
            "PVCurrent":          round(max(0.0, pv_a), 3),
            "PVPower":            round(max(0.0, pv_w), 2),
            "BattVoltage":        round(max(11.5, min(14.8, bv)), 3),
            "BattTemperature":    round(25.0 + random.gauss(0, 0.8), 1),
            "BattChargePower":    round(max(0.0, bcp), 2),
            "BattOverallCurrent": round(boc, 3),
            "BattPercentage":     int(round(max(0, min(100, soc)))),
            "LoadPower":          round(max(0.0, load), 2),
            "CPUPowerDraw":       round(max(0.3, cpu), 3),
            "PowerProfile":       "power-saver",
        }


_SIM = _SimState()


def simulate_data() -> dict:
    """Drop-in replacement for hardware.parse_data() when SIM_MODE=true."""
    return _SIM.step()
