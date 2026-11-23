"""对局入口契约与静态合规检查（不启动 SC2）。"""

from __future__ import annotations

import inspect

from helpers import BOT_PATH, load_bot_module
from sc2.bot_ai import BotAI
from sc2.dicts.unit_trained_from import UNIT_TRAINED_FROM
from sc2.dicts.upgrade_researched_from import UPGRADE_RESEARCHED_FROM
from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.upgrade_id import UpgradeId


def test_entrypoint_is_parameterless_botai_subclass():
    module = load_bot_module()
    assert module.__all__ == ["CompetitiveBot"]
    assert issubclass(module.CompetitiveBot, BotAI)
    signature = inspect.signature(module.CompetitiveBot.__init__)
    assert list(signature.parameters) == ["self"]
    bot = module.CompetitiveBot()
    assert inspect.iscoroutinefunction(bot.on_step)
    assert inspect.iscoroutinefunction(bot.on_start)


def test_uses_expected_terran_tech_requirements():
    # 这些是 Bot 生产/研究逻辑依赖的科技树关系，游戏数据变化时会立刻报警。
    assert UNIT_TRAINED_FROM[UnitTypeId.MARINE] == {UnitTypeId.BARRACKS}
    assert UNIT_TRAINED_FROM[UnitTypeId.MARAUDER] == {UnitTypeId.BARRACKS}
    assert UNIT_TRAINED_FROM[UnitTypeId.SIEGETANK] == {UnitTypeId.FACTORY}
    assert UNIT_TRAINED_FROM[UnitTypeId.MEDIVAC] == {UnitTypeId.STARPORT}
    assert UNIT_TRAINED_FROM[UnitTypeId.VIKINGFIGHTER] == {UnitTypeId.STARPORT}
    assert UPGRADE_RESEARCHED_FROM[UpgradeId.STIMPACK] == UnitTypeId.BARRACKSTECHLAB
    assert (
        UPGRADE_RESEARCHED_FROM[UpgradeId.TERRANINFANTRYWEAPONSLEVEL1]
        == UnitTypeId.ENGINEERINGBAY
    )


def test_no_network_or_opponent_code_access():
    source = BOT_PATH.read_text(encoding="utf-8")
    forbidden = (
        "requests",
        "urllib",
        "socket",
        "aiohttp",
        "httpx",
        "openai",
        "bots/codex",
        "bots/antigravity",
        "bots/other",
        "Starcarft-agent-worktrees/codex",
    )
    for token in forbidden:
        assert token not in source, f"bot.py 不应包含 {token}"


def test_only_visible_enemy_information_is_used():
    source = BOT_PATH.read_text(encoding="utf-8")
    # all_enemy_units 会包含超出正常视野的缓存信息，竞技对局中不使用。
    assert "all_enemy_units" not in source
    # 战略目标只来自“见过的建筑”记忆或当前可见建筑。
    assert "_seen_enemy_structures" in source
