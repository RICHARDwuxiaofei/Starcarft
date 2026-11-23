"""集中放置可调参数；价格、科技前置、武器信息从游戏 API 取得。"""

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BotConfig:
    seed: int = 20261123
    game_step: int = 4
    macro_interval: float = 0.7
    worker_interval: float = 2.0
    strategy_interval: float = 0.7
    telemetry_interval: float = 10.0
    max_workers: int = 72
    max_bases: int = 6
    mobile_memory_seconds: float = 45.0
    structure_memory_seconds: float = 600.0
    attack_supply: float = 42.0
    retreat_ratio: float = 0.7
    attack_ratio: float = 1.25
    # 交换采用本玩家正常 API 分数，矿 + 气等权；这些阈值仍待实战校准。
    trade_window_seconds: float = 30.0
    trade_min_loss: float = 600.0
    trade_loss_ratio: float = 1.8
    regroup_min_seconds: float = 12.0
    regroup_radius: float = 16.0
    regroup_fraction: float = 0.70
    max_placement_queries: int = 4
    debug_overlay: bool = False

    def __post_init__(self):
        if self.game_step < 1 or self.game_step > 32:
            raise ValueError("game_step 必须在 1..32 之间")
        if self.max_workers < 12 or self.max_workers > 90:
            raise ValueError("max_workers 必须在 12..90 之间")
        if self.max_bases < 1:
            raise ValueError("max_bases 必须为正数")
        if self.attack_ratio <= self.retreat_ratio or self.retreat_ratio <= 0:
            raise ValueError("进攻阈值必须大于撤退阈值，撤退阈值必须为正")
        if (
            min(
                self.macro_interval,
                self.worker_interval,
                self.strategy_interval,
                self.telemetry_interval,
                self.mobile_memory_seconds,
                self.structure_memory_seconds,
            )
            <= 0
        ):
            raise ValueError("决策间隔和记忆时长必须为正数")
        if self.max_placement_queries < 1:
            raise ValueError("max_placement_queries 必须为正数")
        if any(
            not math.isfinite(value) or value <= 0
            for value in (
                self.trade_window_seconds,
                self.trade_min_loss,
                self.regroup_min_seconds,
                self.regroup_radius,
            )
        ):
            raise ValueError("交换窗口、损失门槛和重整时间/半径必须是有限正数")
        if not math.isfinite(self.trade_loss_ratio) or self.trade_loss_ratio <= 1:
            raise ValueError("不利交换倍率必须是大于 1 的有限数")
        if not math.isfinite(self.regroup_fraction) or not 0 < self.regroup_fraction <= 1:
            raise ValueError("归队战力比例必须在 (0, 1] 内")
