"""Game-independent macro decisions. Values are tunable heuristics, not build orders."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Signals:
    time: float
    bases: int
    army_supply: float
    local_pressure: float
    enemy_air: float
    enemy_armor: int
    detection_risk: bool


def desired_workers(bases: int, gas_geysers: int) -> int:
    return min(78, max(16, 16 * bases + 3 * gas_geysers + min(4, 2 * bases)))


def supply_order_count(
    used: float, free: float, cap: float, pending: int, producers: int
) -> int:
    """Count only new depots, including a production buffer and pending capacity."""
    if cap >= 200:
        return 0
    buffer = 3 if used < 24 else min(20, 5 + 2 * producers)
    forecast = free + min(pending * 8, 200 - cap)
    starts = 0
    while forecast <= buffer and cap + (pending + starts) * 8 < 200:
        starts += 1
        forecast += 8
    return min(starts, 2 if used >= 65 else 1)


def expansion_allowed(s: Signals) -> bool:
    if s.bases == 0:
        return True
    if s.local_pressure >= 5 or s.bases >= 5:
        return False
    gates = {1: (100, 1), 2: (272, 18), 3: (405, 35), 4: (535, 60)}
    time, army = gates[s.bases]
    return s.time >= time and s.army_supply >= army


def air_targets(s: Signals) -> tuple[int, int]:
    """Medivacs support the ground army; visible air evidence raises Viking output."""
    medivacs = min(7, max(2, int(s.army_supply // 16)))
    vikings = min(14, max(0, int(s.enemy_air * 0.85 + 0.5)))
    return medivacs, vikings
