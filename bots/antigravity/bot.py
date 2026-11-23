"""Entrypoint for the Antigravity competitive Terran bot."""

from __future__ import annotations

import pathlib
import random
import sys
from typing import TYPE_CHECKING

from sc2.bot_ai import BotAI

if __package__:
    from .ag_terran.bases import BaseManager
    from .ag_terran.combat import CombatMicro
    from .ag_terran.config import BotConfig
    from .ag_terran.economy import EconomyManager
    from .ag_terran.intel import IntelTracker
    from .ag_terran.macro import MacroManager
    from .ag_terran.placement import PlacementManager
    from .ag_terran.scout import ScoutManager
    from .ag_terran.tactics import TacticsManager
else:
    _bot_dir = str(pathlib.Path(__file__).resolve().parent)
    if _bot_dir not in sys.path:
        sys.path.insert(0, _bot_dir)
    from ag_terran.bases import BaseManager
    from ag_terran.combat import CombatMicro
    from ag_terran.config import BotConfig
    from ag_terran.economy import EconomyManager
    from ag_terran.intel import IntelTracker
    from ag_terran.macro import MacroManager
    from ag_terran.placement import PlacementManager
    from ag_terran.scout import ScoutManager
    from ag_terran.tactics import TacticsManager

if TYPE_CHECKING:
    from sc2.unit import Unit


class CompetitiveBot(BotAI):
    """Independent Terran bot using a 1-1-1 Reaper expand into Bio-Tank-Medivac-Viking."""

    def __init__(self, seed: int = 42) -> None:
        super().__init__()
        self.seed = seed
        self.rng = random.Random(seed)
        self.config = BotConfig()
        self.intel = IntelTracker(self.config)
        self.bases = BaseManager(self.config)
        self.placement = PlacementManager()
        self.economy = EconomyManager(self.config)
        self.macro = MacroManager(self.config)
        self.scout = ScoutManager(self.config)
        self.tactics = TacticsManager(self.config)
        self.combat = CombatMicro(self.config)
        self._initialized = False

    async def on_start(self) -> None:
        """Initialize map-dependent subsystems once game info is available."""
        self.intel.initialize(self)
        self.bases.initialize(self)
        self.placement.initialize(self)
        self.scout.initialize(self)
        self._initialized = True

    async def on_step(self, iteration: int) -> None:
        """Execute one deterministic decision tick across all bot subsystems."""
        if not self._initialized:
            await self.on_start()

        self.intel.update(self)
        self.bases.update(self, self.intel)
        self.economy.update(self, self.intel, self.bases, self.scout.scout_worker_tag)
        await self.macro.update(
            self,
            self.intel,
            self.bases,
            self.placement,
            self.economy,
            self.scout.scout_worker_tag,
        )
        self.scout.update(self, self.intel, self.bases, self.economy)
        reserved_scouts = self.scout.get_reserved_unit_tags()
        self.tactics.update(self, self.intel, self.bases, reserved_scouts)
        self.combat.update(
            self,
            self.intel,
            self.bases,
            self.placement,
            self.tactics,
            reserved_scouts,
        )

    async def on_unit_destroyed(self, unit_tag: int) -> None:
        """Clean up unit references across intel, economy, scouts, and tank tracking."""
        self.intel.on_unit_destroyed(unit_tag)
        self.economy.on_unit_destroyed(unit_tag)
        self.scout.on_unit_destroyed(unit_tag)
        self.combat.on_unit_destroyed(unit_tag)

    async def on_enemy_unit_entered_vision(self, unit: Unit) -> None:
        """Immediately record enemy units when they enter legal vision."""
        self.intel.on_enemy_unit_entered_vision(self, unit)
