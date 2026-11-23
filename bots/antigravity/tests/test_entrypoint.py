"""Entrypoint, isolation, determinism, and fair-play static tests."""

from __future__ import annotations

import ast
import importlib.util
import pathlib

from sc2.bot_ai import BotAI

BOT_ROOT = pathlib.Path(__file__).resolve().parents[1]
BOT_FILE = BOT_ROOT / "bot.py"

FORBIDDEN_IMPORTS = {
    "aiohttp",
    "anthropic",
    "google",
    "httpx",
    "openai",
    "requests",
    "socket",
    "subprocess",
    "urllib",
}


def test_package_import_and_zero_arg_instantiation() -> None:
    from bots.antigravity.bot import CompetitiveBot

    assert issubclass(CompetitiveBot, BotAI)
    bot = CompetitiveBot()
    assert isinstance(bot, BotAI)
    assert bot.seed == 42


def test_isolated_spec_import_and_zero_arg_instantiation() -> None:
    spec = importlib.util.spec_from_file_location(
        "isolated_antigravity_bot", str(BOT_FILE)
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cls = mod.CompetitiveBot
    assert issubclass(cls, BotAI)
    bot = cls()
    assert isinstance(bot, BotAI)


def test_deterministic_rng_with_same_seed() -> None:
    from bots.antigravity.bot import CompetitiveBot

    b1 = CompetitiveBot(seed=12345)
    b2 = CompetitiveBot(seed=12345)
    assert [b1.rng.random() for _ in range(10)] == [b2.rng.random() for _ in range(10)]


def test_fair_play_no_network_or_debug_cheats() -> None:
    py_files = [p for p in BOT_ROOT.rglob("*.py") if "tests" not in p.parts]
    assert py_files
    for path in py_files:
        source = path.read_text(encoding="utf-8")
        assert "debug_" not in source, f"Forbidden debug call in {path}"
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    assert top not in FORBIDDEN_IMPORTS, (
                        f"Forbidden import {top} in {path}"
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                top = node.module.split(".")[0]
                assert top not in FORBIDDEN_IMPORTS, f"Forbidden import {top} in {path}"
