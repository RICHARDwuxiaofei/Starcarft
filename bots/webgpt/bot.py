"""webGPT / Bastion：原创、纯本地规则驱动的人族对战 Bot。

入口约定：from bots.webgpt.bot import CompetitiveBot。
此文件负责观察、调度和记录；战术与经济分别位于本目录的独立模块中。
导入或实例化不会启动 SC2，也不会联网调用大模型。
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import asdict, replace
from pathlib import Path
from time import perf_counter

from sc2.bot_ai import BotAI
from sc2.data import Race
from sc2.ids.unit_typeid import UnitTypeId as U

from .combat import CombatController
from .config import BotConfig
from .economy import WorkerManager
from .intelligence import Intelligence
from .macro import MacroManager
from .runtime import CommandBus
from .strategy import Strategist

LOG = logging.getLogger("webgpt")


class Telemetry:
    """只写自己的游戏观察/决策；磁盘故障不改变对战逻辑。"""

    def __init__(self, path: Path | None):
        self.path = Path(path) if path is not None else None
        self.error: str | None = None

    def write(self, event: str, game_time: float, **fields):
        if self.path is None or self.error is not None:
            return
        record = {"event": event, "game_time": round(game_time, 3), **fields}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            self.error = str(exc)
            LOG.warning("战术日志写入失败，Bot 将继续比赛：%s", exc)


class CompetitiveBot(BotAI):
    """无参数构造即可参赛；可选 seed 和独立日志路径供中立裁判传入。"""

    NAME = "webGPT Bastion"
    VERSION = "1.0.0"

    def __init__(
        self,
        seed: int | None = None,
        telemetry_path: Path | None = None,
        config: BotConfig | None = None,
    ):
        super().__init__()
        self.config = config or BotConfig()
        if seed is not None:
            self.config = replace(self.config, seed=seed)
        self.rng = random.Random(self.config.seed)
        # 明确采用构造命令、总线统一提交的模式；禁止 Unit 方法提前自动执行。
        self.unit_command_uses_self_do = True
        self.bus = CommandBus(self)
        self.workers_manager = WorkerManager(self.config)
        self.macro = MacroManager(self.config, self.workers_manager)
        self.intelligence = Intelligence(self.config)
        self.strategist = Strategist(self.config)
        self.combat = CombatController(self.config)
        self.telemetry = Telemetry(telemetry_path)
        self.plan = None
        self.match_error: str | None = None
        self.last_game_time = 0.0
        self.game_result: str | None = None
        self._next_macro = self._next_workers = self._next_strategy = 0.0
        self._next_telemetry = 0.0
        self._step_count = 0
        self._step_total_ms = self._step_max_ms = 0.0

    async def on_start(self):
        try:
            if self.race != Race.Terran:
                raise ValueError("webGPT Bastion 需要以 Race.Terran 加入游戏")
            self.client.game_step = self.config.game_step
            self.macro.initialize(self)
            self.intelligence.update(self)
            self.plan = self.strategist.update(self, self.intelligence)
            self.telemetry.write(
                "start",
                self.time,
                name=self.NAME,
                version=self.VERSION,
                config=asdict(self.config),
                map_name=self.game_info.map_name,
                player_id=self.player_id,
                start_location=list(self.start_location),
            )
        except Exception as exc:
            self._record_error(exc, "start")
            raise

    async def on_step(self, iteration: int):
        started = perf_counter()
        self.last_game_time = self.time
        try:
            self.bus.begin_frame()
            # 军队每帧需要技能信息；结构只在运营帧查询，避免无意义的重复RPC。
            casters = list(self.units.exclude_type({U.SCV, U.MULE}))
            if self.time >= self._next_macro:
                casters.extend(self.structures.ready)
            await self.bus.prime(casters)
            if self.plan is None or self.time >= self._next_strategy:
                self.intelligence.update(self)
                previous = self.plan
                self.plan = self.strategist.update(self, self.intelligence)
                if previous is None or previous.stance != self.plan.stance:
                    self.telemetry.write(
                        "stance",
                        self.time,
                        stance=self.plan.stance.value,
                        reason=self.plan.reason,
                        objective=list(self.plan.objective),
                    )
                self._next_strategy = self.time + self.config.strategy_interval
            report = self.intelligence.report
            if self.time >= self._next_macro:
                await self.macro.step(self, self.bus, self.plan, report)
                self._next_macro = self.time + self.config.macro_interval
            if self.time >= self._next_workers:
                await self.workers_manager.step(self, self.bus, self.plan, report)
                self._next_workers = self.time + self.config.worker_interval
            # 运营可向地堡分配士兵，战斗模块尊重总线标记，绝不覆盖该指令。
            await self.combat.step(self, self.bus, self.plan, report)
            elapsed = (perf_counter() - started) * 1000
            self._step_count += 1
            self._step_total_ms += elapsed
            self._step_max_ms = max(self._step_max_ms, elapsed)
            if self.time >= self._next_telemetry:
                self._snapshot(elapsed)
                self._next_telemetry = self.time + self.config.telemetry_interval
        except Exception as exc:
            self._record_error(exc, "step")
            # 让裁判看见失败；不使用吞掉所有异常、假装Bot继续正常工作的兜底。
            raise

    def _snapshot(self, elapsed):
        self.telemetry.write(
            "snapshot",
            self.time,
            minerals=self.minerals,
            gas=self.vespene,
            supply_used=self.supply_used,
            supply_cap=self.supply_cap,
            workers=self.workers.amount,
            army_supply=self.supply_army,
            bases=self.townhalls.amount,
            stance=self.plan.stance.value,
            reason=self.plan.reason,
            composition=asdict(self.plan.composition),
            visible_enemies=self.intelligence.report.visible_enemies,
            remembered_contacts=len(self.intelligence.contacts),
            step_ms=round(elapsed, 2),
            mean_step_ms=round(self._step_total_ms / max(1, self._step_count), 2),
            max_step_ms=round(self._step_max_ms, 2),
            commands=dict(self.bus.metrics),
        )

    def _record_error(self, exc: Exception, phase: str):
        self.match_error = f"{phase}: {type(exc).__name__}: {exc}"
        self.telemetry.write("error", self.last_game_time, error=self.match_error)
        LOG.exception("webGPT Bot 运行错误 (%s)", phase)

    async def on_unit_destroyed(self, unit_tag: int):
        self.intelligence.forget(unit_tag)
        self.workers_manager.jobs.pop(unit_tag, None)
        self.macro.blocked_addons.pop(unit_tag, None)

    async def on_end(self, game_result):
        self.game_result = getattr(game_result, "name", str(game_result))
        self.telemetry.write(
            "end",
            self.last_game_time,
            result=self.game_result,
            match_error=self.match_error,
            commands=dict(self.bus.metrics),
        )
