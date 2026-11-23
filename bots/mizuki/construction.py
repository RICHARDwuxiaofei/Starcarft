"""Site-local recovery for interrupted Terran Command Center foundations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sc2.ids.ability_id import AbilityId as A
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2

from .evacuation import MineralLineEvacuation, SiegeZone

if TYPE_CHECKING:
    from sc2.bot_ai import BotAI
    from sc2.unit import Unit


@dataclass
class Foundation:
    progress: float
    advanced_at: float
    queued_worker: int | None = None
    queued_at: float = 0.0
    acknowledged: bool = False
    last_unsafe: float = float("-inf")
    stayed_idle: bool = False
    site_was_unsafe: bool = False
    retry_worker_after: dict[int, float] = field(default_factory=dict)


def _works_on(worker: Unit, center: Unit) -> bool:
    for order in worker.orders:
        target, ability = order.target, order.ability.id
        if (
            isinstance(target, Point2)
            and ability == A.TERRANBUILD_COMMANDCENTER
            and target.distance_to(center.position) <= 1.0
        ):
            return True
        if target == center.tag and (
            ability in {A.SMART, A.EFFECT_REPAIR, A.EFFECT_REPAIR_SCV}
            or worker.is_constructing_scv
        ):
            return True
    return False


@dataclass
class CommandCenterRecovery:
    jobs: dict[int, Foundation] = field(default_factory=dict)

    def reserved(self, tag: int) -> bool:
        return any(job.queued_worker == tag for job in self.jobs.values())

    @property
    def awaiting_worker_order(self) -> bool:
        return any(
            j.queued_worker is not None and not j.acknowledged
            for j in self.jobs.values()
        )

    def update(
        self,
        bot: BotAI,
        enemies: list[Unit],
        evac: MineralLineEvacuation,
        trace: Callable[..., None],
    ) -> None:
        now = bot.time
        centers = sorted(
            (
                h
                for h in bot.townhalls
                if h.type_id == U.COMMANDCENTER and not h.is_ready and not h.is_flying
            ),
            key=lambda h: h.tag,
        )
        live = {h.tag for h in centers}
        for tag in self.jobs.keys() - live:
            del self.jobs[tag]
        if not centers:
            return
        workers = {w.tag: w for w in bot.workers}
        claimed = {
            j.queued_worker for j in self.jobs.values() if j.queued_worker is not None
        }
        # Use ordinary visible ground-fire sources plus the pre-existing
        # evacuation mask. A scout at another base is not a global veto.
        threats = MineralLineEvacuation(
            zones={
                e.tag: SiegeZone(
                    e.position, max(5.5, e.ground_range + e.radius + 2.5), now
                )
                for e in enemies
                if (e.can_attack_ground or e.type_id == U.BUNKER)
                and e.type_id not in {U.SCV, U.PROBE, U.DRONE, U.MULE}
                and not (e.is_cloaked and not e.is_revealed)
            }
        )
        for center in centers:
            p = center.position
            job = self.jobs.setdefault(
                center.tag, Foundation(center.build_progress, now)
            )
            if center.build_progress > job.progress:
                job.progress, job.advanced_at = center.build_progress, now
                if job.stayed_idle:
                    trace(
                        "cc_construction_restarted",
                        tag=center.tag,
                        progress=round(job.progress, 3),
                    )
                    job.stayed_idle = False
            assigned = [w for w in workers.values() if _works_on(w, center)]
            site_unsafe = min(threats.margin(p), evac.margin(p)) < 2
            if site_unsafe:
                job.last_unsafe = now
                if not job.site_was_unsafe:
                    trace("cc_site_threat_seen", tag=center.tag)
            elif job.site_was_unsafe:
                trace("cc_site_threat_clear", tag=center.tag)
            job.site_was_unsafe = site_unsafe
            if job.queued_worker is not None:
                worker = workers.get(job.queued_worker)
                queued_for = now - job.queued_at
                job.acknowledged = worker in assigned
                if not worker or (worker not in assigned and queued_for >= 1.0):
                    if worker is not None:
                        job.retry_worker_after[worker.tag] = now + 3
                    claimed.discard(job.queued_worker)
                    job.queued_worker = None
            if not assigned and now - job.advanced_at < 1.25:
                continue
            if assigned:
                # Gen8 evacuation has priority. Additional protection applies
                # only to the builder of a foundation under direct attack.
                for worker in assigned:
                    if (
                        worker.tag in evac.sheltered
                        or worker.tag in bot.unit_tags_received_action
                    ):
                        continue
                    if threats.avoids_fire(worker.position, p):
                        continue
                    if threats.margin(worker.position) >= 0:
                        # An approaching builder can wait outside a newly unsafe
                        # path instead of following the stale Smart order into fire.
                        if worker.stop():
                            trace(
                                "cc_builder_waits_for_clear_route",
                                tag=center.tag,
                                worker=worker.tag,
                            )
                        continue
                    refuge = [
                        h
                        for h in bot.townhalls.ready
                        if not h.is_flying
                        and min(threats.margin(h.position), evac.margin(h.position))
                        >= 2
                    ]
                    # Honor tank/bio zones as well as the direct construction threat.
                    combined = MineralLineEvacuation(
                        zones={**threats.zones, **evac.zones}
                    )
                    waypoint = combined._escape(bot, worker, refuge)
                    if waypoint is not None and worker.move(waypoint):
                        job.last_unsafe = now
                        trace("cc_builder_evacuated", tag=center.tag, worker=worker.tag)
                continue
            if not job.stayed_idle:
                job.stayed_idle = True
                trace(
                    "cc_construction_stalled",
                    tag=center.tag,
                    progress=round(job.progress, 3),
                )
            if job.queued_worker is not None or now - job.last_unsafe < 4:
                continue
            candidates = sorted(
                (
                    w
                    for w in workers.values()
                    if w.tag not in claimed
                    and now >= job.retry_worker_after.get(w.tag, 0)
                    and w.tag not in evac.sheltered
                    and w.tag not in bot.unit_tags_received_action
                    and (w.is_collecting or w.is_idle)
                    and w.distance_to(p) <= 40
                    and threats.avoids_fire(w.position, p, clearance=1.0)
                    and evac.avoids_fire(w.position, p, clearance=1.0)
                ),
                key=lambda w: (w.distance_to(p), w.tag),
            )
            for worker in candidates[:3]:
                if worker.smart(center):
                    job.queued_worker, job.queued_at = worker.tag, now
                    job.acknowledged = False
                    claimed.add(worker.tag)
                    trace("cc_resume_queued", tag=center.tag, worker=worker.tag)
                    # Give other macro actions a stable, single choice this frame.
                    return
