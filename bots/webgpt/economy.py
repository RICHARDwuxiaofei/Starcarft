"""SCV 角色管理与资源分配，独立实现，不调用示例 Bot 的采矿策略。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from sc2.ids.unit_typeid import UnitTypeId as U


@dataclass(slots=True)
class ResourceSlot:
    unit: object
    capacity: int
    gas: bool


def choose_resource(worker, slots: list[ResourceSlot], loads: Counter):
    """优先未饱和资源，再比较路程；标签提供稳定的平局决策。"""
    if not slots:
        return None
    return min(
        slots,
        key=lambda slot: (
            max(0, loads[slot.unit.tag] - slot.capacity + 1),
            worker.distance_to(slot.unit) + loads[slot.unit.tag] * 1.5,
            slot.unit.tag,
        ),
    )


class WorkerManager:
    def __init__(self, config):
        self.config = config
        self.jobs: dict[int, int] = {}
        self.scout_tag: int | None = None
        self.scout_index = 0
        self.next_scout_at = 45.0
        self.scout_started = 0.0
        self.repair_tags: set[int] = set()

    @property
    def reserved_tags(self) -> set[int]:
        return self.repair_tags | ({self.scout_tag} if self.scout_tag else set())

    def builder(self, bot, bus, position):
        candidates = [
            w
            for w in bot.workers
            if w.tag not in bus.issued
            and w.tag not in self.reserved_tags
            and not w.is_constructing_scv
            and not w.is_repairing
            and (w.is_idle or w.is_collecting)
        ]
        if not candidates:
            return None
        return min(
            candidates, key=lambda w: (w.is_carrying_resource, w.distance_to(position), w.tag)
        )

    async def step(self, bot, bus, plan, intel):
        self.repair_tags.clear()
        workers = list(bot.workers)
        live = {w.tag for w in workers}
        self.jobs = {tag: job for tag, job in self.jobs.items() if tag in live}
        if self.scout_tag is not None and self.scout_tag not in live:
            self.scout_tag = None
            self.next_scout_at = max(self.next_scout_at, bot.time + 120)
        self._evacuate(bot, bus, plan)
        self._resume_buildings(bot, bus)
        self._repair(bot, bus, plan)
        self._scout(bot, bus, plan)
        self._mine(bot, bus)

    def _evacuate(self, bot, bus, plan):
        """矿线遭袭时保住工人；不把正在建造的 SCV 一律强行拉走。"""
        threats = [
            e
            for e in bot.enemy_units
            if e.is_visible and e.can_attack_ground and not e.is_structure
        ]
        if not threats:
            return
        safe_bases = [
            b
            for b in bot.townhalls.ready
            if not b.is_flying and not any(e.distance_to(b) < 13 for e in threats)
        ]
        for worker in bot.workers:
            if worker.tag in bus.issued:
                continue
            close = [e for e in threats if e.distance_to(worker) < 6]
            if not close or (worker.is_constructing_scv and worker.health_percentage > 0.5):
                continue
            if safe_bases:
                destination = min(safe_bases, key=lambda b: b.distance_to(worker)).position
            else:
                enemy = min(close, key=lambda e: e.distance_to(worker))
                destination = worker.position.towards(enemy.position, -4)
                if not bot.in_pathing_grid(destination):
                    destination = plan.fallback
            bus.issue(worker.move(destination))

    def _resume_buildings(self, bot, bus):
        for structure in bot.structures.not_ready:
            # 附件由母建筑制造，不能派工人接手。
            if structure.type_id in {
                U.BARRACKSTECHLAB,
                U.BARRACKSREACTOR,
                U.FACTORYTECHLAB,
                U.FACTORYREACTOR,
                U.STARPORTTECHLAB,
                U.STARPORTREACTOR,
            }:
                continue
            if any(w.is_constructing_scv and w.distance_to(structure) < 4 for w in bot.workers):
                continue
            if any(
                e.is_visible and e.can_attack_ground and e.distance_to(structure) < 7
                for e in bot.enemy_units
            ):
                continue
            worker = self.builder(bot, bus, structure.position)
            if worker:
                bus.issue(worker.smart(structure))

    def _repair(self, bot, bus, plan):
        if bus.budget.minerals < 60:
            return
        candidates = [
            s
            for s in bot.structures.ready
            if s.is_mechanical and not s.is_flying and s.health_percentage < 0.82
        ]
        candidates += [
            u
            for u in bot.units.of_type(
                {U.SIEGETANK, U.SIEGETANKSIEGED, U.MEDIVAC, U.VIKINGFIGHTER, U.RAVEN}
            )
            if u.health_percentage < 0.65
            and bot.townhalls
            and bot.townhalls.closest_distance_to(u) < 15
        ]
        for target in sorted(candidates, key=lambda u: (u.health_percentage, u.tag))[:2]:
            danger = any(
                e.is_visible and e.can_attack_ground and e.distance_to(target) < 7
                for e in bot.enemy_units
            )
            if danger and target.type_id != U.BUNKER:
                continue
            repairers = [
                w
                for w in bot.workers
                if w.tag not in bus.issued
                and w.tag != self.scout_tag
                and not w.is_constructing_scv
                and w.distance_to(target) < 18
                and (w.is_idle or w.is_collecting or w.is_repairing)
            ]
            amount = 3 if target.type_id == U.BUNKER and plan.emergency else 1
            for worker in sorted(repairers, key=lambda w: (w.distance_to(target), w.tag))[:amount]:
                self.repair_tags.add(worker.tag)
                bus.issue(worker.repair(target))

    def _scout(self, bot, bus, plan):
        if (
            self.scout_tag is None
            and bot.time >= self.next_scout_at
            and bot.time < 320
            and bot.workers.amount >= 16
            and not plan.emergency
        ):
            worker = self.builder(bot, bus, bot.start_location)
            if worker:
                self.scout_tag = worker.tag
                self.scout_started = bot.time
                self.scout_index = 0
                self.jobs.pop(worker.tag, None)
        if self.scout_tag is None:
            return
        scout = bot.workers.find_by_tag(self.scout_tag)
        if scout is None:
            self.scout_tag = None
            self.next_scout_at = bot.time + 90
            return
        danger = any(
            e.is_visible and e.can_attack_ground and e.distance_to(scout) < 7
            for e in bot.enemy_units | bot.enemy_structures
        )
        if (
            danger
            or scout.health_percentage < 0.6
            or plan.emergency
            or bot.time - self.scout_started > 110
        ):
            bus.issue(scout.move(plan.fallback))
            self.scout_tag = None
            self.next_scout_at = bot.time + 120
            return
        enemy_starts = list(bot.enemy_start_locations)
        if not enemy_starts:
            return
        expansions = sorted(
            bot.expansion_locations_list, key=lambda p: (p.distance_to(enemy_starts[0]), p.x, p.y)
        )
        route = enemy_starts + [
            p for p in expansions[:4] if min(p.distance_to(s) for s in enemy_starts) > 8
        ]
        target = route[self.scout_index % len(route)]
        if scout.distance_to(target) < 6:
            self.scout_index += 1
            target = route[self.scout_index % len(route)]
        if scout.tag not in bus.issued:
            bus.issue(scout.move(target))

    def _mine(self, bot, bus):
        bases = [b for b in bot.townhalls.ready if not b.is_flying]
        if not bases:
            return
        slots = [
            ResourceSlot(m, 2, False)
            for m in bot.mineral_field
            if m.mineral_contents > 0 and any(m.distance_to(b) < 10 for b in bases)
        ]
        gas_workers = 1 if bot.vespene > max(400, bot.minerals * 1.8) else 3
        slots += [
            ResourceSlot(g, gas_workers, True)
            for g in bot.gas_buildings.ready
            if g.vespene_contents > 0 and any(g.distance_to(b) < 12 for b in bases)
        ]
        if not slots:
            # 矿枯竭、扩张尚未完成时可长距离采矿；不在敌军近旁采集。
            slots = [
                ResourceSlot(m, 2, False)
                for m in bot.mineral_field
                if m.mineral_contents > 0
                and not any(
                    e.is_visible and e.can_attack_ground and e.distance_to(m) < 10
                    for e in bot.enemy_units
                )
            ]
            slots.sort(key=lambda s: min(s.unit.distance_to(b) for b in bases))
            slots = slots[:8]
        resources = {s.unit.tag: s for s in slots}
        workers = [
            w
            for w in bot.workers
            if w.tag not in bus.issued
            and w.tag not in self.reserved_tags
            and not w.is_constructing_scv
            and not w.is_repairing
            and (w.is_idle or w.is_collecting)
        ]
        # 利用自己的记账保留正常返矿工人的资源归属，避免每次返矿都换矿。
        assignments: dict[int, int] = {}
        for worker in workers:
            order_target = worker.orders[0].target if worker.orders else None
            if isinstance(order_target, int) and order_target in resources:
                assignments[worker.tag] = order_target
            elif self.jobs.get(worker.tag) in resources:
                assignments[worker.tag] = self.jobs[worker.tag]
        loads = Counter(assignments.values())
        # 空闲/未分配者先处理，已有岗位的工人只有超饱和才重新分配。
        workers.sort(key=lambda w: (not w.is_idle, w.tag))
        for worker in workers:
            old = assignments.get(worker.tag)
            if old is not None and loads[old] <= resources[old].capacity and not worker.is_idle:
                self.jobs[worker.tag] = old
                continue
            if old is not None:
                loads[old] -= 1
            selected = choose_resource(worker, slots, loads)
            if selected is None:
                continue
            loads[selected.unit.tag] += 1
            self.jobs[worker.tag] = selected.unit.tag
            if worker.is_carrying_resource:
                # 先返还手上资源，下个分配周期再转移；不用互相覆盖的队列指令。
                if worker.is_idle or old != selected.unit.tag:
                    bus.issue(worker.return_resource())
            elif worker.is_idle or old != selected.unit.tag:
                bus.issue(worker.gather(selected.unit))
        for mule in bot.units(U.MULE).idle:
            minerals = [s.unit for s in slots if not s.gas]
            if minerals:
                bus.issue(mule.gather(min(minerals, key=lambda m: mule.distance_to(m))))
