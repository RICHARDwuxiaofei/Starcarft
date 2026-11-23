"""模块间只交换公开观察和决策，不读取对手代码或雾中的真实状态。"""

from dataclasses import dataclass, field
from enum import StrEnum

from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2


class Stance(StrEnum):
    HOLD = "hold"
    DEFEND = "defend"
    ATTACK = "attack"
    RETREAT = "retreat"
    SEARCH = "search"


@dataclass(frozen=True, slots=True)
class Contact:
    tag: int
    type_id: UnitTypeId
    position: Point2
    seen_at: float
    is_structure: bool
    is_flying: bool
    supply: float
    power: float
    is_armored: bool = False
    can_attack_ground: bool = False
    can_attack_air: bool = False


@dataclass(frozen=True, slots=True)
class IntelReport:
    air_supply: float = 0.0
    ground_supply: float = 0.0
    armored_ground_supply: float = 0.0
    enemy_power: float = 0.0
    enemy_bases: int = 0
    cloak_risk: bool = False
    rush: bool = False
    home_threat: Point2 | None = None
    home_threat_power: float = 0.0
    known_structures: tuple[Point2, ...] = ()
    visible_enemies: int = 0
    colossi: int = 0


@dataclass(frozen=True, slots=True)
class Composition:
    marauder_fraction: float = 0.25
    tank_goal: int = 6
    medivac_goal: int = 4
    viking_goal: int = 0
    raven_goal: int = 0


@dataclass(frozen=True, slots=True)
class BattlePlan:
    stance: Stance
    objective: Point2
    rally: Point2
    fallback: Point2
    composition: Composition = field(default_factory=Composition)
    desired_bases: int = 2
    desired_barracks: int = 3
    desired_factories: int = 1
    desired_starports: int = 1
    allow_expand: bool = True
    save_scan: bool = False
    emergency: bool = False
    reason: str = "opening"
