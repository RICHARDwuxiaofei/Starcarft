"""由本玩家正常观察里的累计分数计算近期交换，不读取对方观察或雾中单位。

这里比较的是矿物加瓦斯的资源数值，不是战斗模拟或胜率。己方只计军队
损失，敌方击杀收益包含军队、经济和建筑等类别，避免拆家有收益时误判。
生产新兵不会抵消累计损失；工人或 MULE 的经济类损失不进入止损条件。
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sc2.score import ScoreDetails


_CATEGORIES = ("none", "army", "economy", "technology", "upgrade")
_COUNTERS = ("lost_minerals_army", "lost_vespene_army") + tuple(
    f"killed_{resource}_{category}"
    for resource in ("minerals", "vespene")
    for category in _CATEGORIES
)


@dataclass(frozen=True, slots=True)
class TradeReport:
    available: bool = False
    loss: float = 0.0
    gain: float = 0.0
    seconds: float = 0.0

    def is_bad(self, min_loss: float = 600.0, loss_ratio: float = 1.8) -> bool:
        """需要实质损失且交换劣势才触发；零损失、等倍率交换不触发。"""
        return self.available and self.loss >= min_loss and self.loss > self.gain * loss_ratio


@dataclass(frozen=True, slots=True)
class _Sample:
    time: float
    counters: tuple[float, ...]


class RollingTrade:
    """保留窗口内的累计样本，只统计起止都落在窗口内的采样间隔。

    不知道两个观察之间的逐次死亡时间，因此不对跨越窗口边界的区间做
    比例分摊。正常 0.7 秒战略采样下最多漏计边界的一个间隔；长时间中断
    后只建立新基线，不把整段历史损失冒充最近 30 秒的损失。
    """

    def __init__(self, window_seconds: float = 30.0):
        if not math.isfinite(window_seconds) or window_seconds <= 0:
            raise ValueError("交换统计窗口必须是有限正数")
        self.window_seconds = float(window_seconds)
        self._samples: deque[_Sample] = deque()

    def reset(self) -> None:
        """开始新一轮推进时丢弃上次进攻、撤退和防守的分数历史。"""
        self._samples.clear()

    @staticmethod
    def _read(score: ScoreDetails | None) -> tuple[float, ...] | None:
        try:
            values = tuple(float(getattr(score, field)) for field in _COUNTERS)
        except (AttributeError, TypeError, ValueError, OverflowError):
            return None
        if any(not math.isfinite(value) or value < 0 for value in values):
            return None
        return values

    def update(self, now: float, score: ScoreDetails | None) -> TradeReport:
        """传入本玩家 ``bot.state.score``；首个有效样本仅建基线。"""
        values = self._read(score)
        if not math.isfinite(now) or now < 0 or values is None:
            self.reset()
            return TradeReport()

        if self._samples:
            previous = self._samples[-1]
            # 按字段校验，避免一种分数下降被另一种分数增加掩盖。
            if now < previous.time or any(
                current < old for current, old in zip(values, previous.counters, strict=True)
            ):
                self.reset()
            elif now == previous.time:
                # 同一帧重复观察允许更新数值，不生成零时长的额外区间。
                self._samples.pop()

        self._samples.append(_Sample(float(now), values))
        cutoff = now - self.window_seconds
        while self._samples[0].time < cutoff:
            self._samples.popleft()
        baseline = self._samples[0]
        return TradeReport(
            available=True,
            loss=sum(values[:2]) - sum(baseline.counters[:2]),
            gain=sum(values[2:]) - sum(baseline.counters[2:]),
            seconds=float(now) - baseline.time,
        )
