"""Synthetic game-state builders for offline planner tests.

No SC2 instance is started; these factories create plain dataclass observations
that mirror what the BotAI adapter produces each frame.
"""

from __future__ import annotations

from bots.hunyuan.model import Memory, Snapshot, UnitInfo


def unit(
    tag: int,
    kind: str,
    x: float = 0.0,
    y: float = 0.0,
    *,
    hp: float = 100.0,
    hp_max: float = 100.0,
    progress: float = 1.0,
    energy: float = 0.0,
    addon: str | None = None,
    role: str = "",
) -> UnitInfo:
    return UnitInfo(
        tag=tag,
        kind=kind,
        x=x,
        y=y,
        hp=hp,
        hp_max=hp_max,
        progress=progress,
        energy=energy,
        addon=addon,
        role=role,
        idle=True,
    )


def snapshot(
    time: float = 0.0,
    minerals: int = 500,
    vespene: int = 300,
    supply_used: int = 20,
    supply_cap: int = 38,
    **kwargs,
) -> Snapshot:
    defaults = {
        "time": time,
        "minerals": minerals,
        "vespene": vespene,
        "supply_used": supply_used,
        "supply_cap": supply_cap,
        "supply_left": supply_cap - supply_used,
        "units": [],
        "enemy_visible": [],
        "enemy_memory": [],
        "expansion_points": [(40.0, 40.0), (120.0, 120.0), (10.0, 90.0)],
        "start": (10.0, 10.0),
        "enemy_start": (150.0, 150.0),
        "map_center": (80.0, 80.0),
        "pending": {},
        "upgrades": set(),
        "alerts": set(),
    }
    defaults.update(kwargs)
    return Snapshot(**defaults)


def opened_base() -> list[UnitInfo]:
    """A modest established economy: CC, depot, barracks, factory, starport."""
    return [
        unit(1, "COMMANDCENTER", 10, 10, hp=1500, hp_max=1500, role="structure"),
        unit(2, "SUPPLYDEPOT", 14, 12, role="structure"),
        unit(3, "BARRACKS", 16, 13, addon="techlab", role="structure"),
        unit(4, "FACTORY", 18, 14, addon="techlab", role="structure"),
        unit(5, "STARPORT", 20, 15, addon="reactor", role="structure"),
        unit(6, "ENGINEERINGBAY", 16, 18, role="structure"),
        unit(7, "REFINERY", 13, 8, role="structure"),
    ] + [unit(100 + i, "SCV", 11 + i % 4, 9 + i % 3, role="worker") for i in range(18)]


def combat_army(count: int = 20, tag_start: int = 1000) -> list[UnitInfo]:
    result = []
    for i in range(count):
        kind = "MARINE" if i % 3 else "MARAUDER"
        result.append(unit(tag_start + i, kind, 30 + i * 0.3, 30, role="army"))
    for i in range(max(1, count // 8)):
        result.append(unit(tag_start + 500 + i, "MEDIVAC", 32 + i, 30, role="army"))
    for i in range(max(1, count // 10)):
        result.append(unit(tag_start + 700 + i, "SIEGETANK", 34 + i, 30, role="army"))
    return result


def memory(seed: int = 1, **kwargs) -> Memory:
    defaults = {"seed": seed}
    defaults.update(kwargs)
    return Memory(**defaults)
