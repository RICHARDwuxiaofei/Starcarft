from collections import Counter
from types import SimpleNamespace

from bots.webgpt.economy import ResourceSlot, WorkerManager, choose_resource
from sc2.position import Point2


def worker(tag, x=0, **attributes):
    defaults = dict(
        tag=tag,
        is_constructing_scv=False,
        is_repairing=False,
        is_idle=False,
        is_collecting=True,
        is_carrying_resource=False,
        position=Point2((x, 0)),
    )
    defaults.update(attributes)
    item = SimpleNamespace(**defaults)
    item.distance_to = lambda target: item.position.distance_to(getattr(target, "position", target))
    return item


def test_balancing_selects_remote_free_minerals_before_overstacking_home():
    patch1, patch2 = worker(10, 2), worker(20, 15)
    slots = [ResourceSlot(patch1, 2, False), ResourceSlot(patch2, 2, False)]
    assert choose_resource(worker(1), slots, Counter({10: 2})).unit.tag == 20


def test_resource_tie_break_is_stable_and_empty_mineral_list_is_safe():
    patches = [ResourceSlot(worker(20, 2), 2, False), ResourceSlot(worker(10, 2), 2, False)]
    assert choose_resource(worker(1), patches, Counter()).unit.tag == 10
    assert choose_resource(worker(1), [], Counter()) is None


def test_builder_respects_scout_repairer_construction_and_frame_claims():
    manager = WorkerManager(None)
    manager.scout_tag = 1
    manager.repair_tags = {2}
    units = [worker(1), worker(2), worker(3, is_constructing_scv=True), worker(4), worker(5, 5)]
    bot = SimpleNamespace(workers=units)
    bus = SimpleNamespace(issued={4})
    assert manager.builder(bot, bus, Point2((0, 0))).tag == 5


def test_builder_prefers_empty_hands_over_discarding_loaded_workers_trip():
    manager = WorkerManager(None)
    bot = SimpleNamespace(workers=[worker(1, 1, is_carrying_resource=True), worker(2, 4)])
    assert manager.builder(bot, SimpleNamespace(issued=set()), Point2((0, 0))).tag == 2
