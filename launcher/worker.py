"""Run one real SC2 game. Only invoked after the GUI Start action.

Competitors are imported under separate synthetic packages, so both may contain
relative imports such as 'from .config import ...' without name collisions.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import inspect
import json
import os
import sys
import time
import traceback
import types
from datetime import datetime
from pathlib import Path

def timestamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")

def load_bot(entry: dict, alias: str):
    source = Path(entry["path"]).resolve()
    if not source.is_file() or source.suffix.lower() != ".py":
        raise ValueError("Bot 脚本不是可用的 .py 文件: " + str(source))
    folder = source.parent
    init_file = folder / "__init__.py"
    if init_file.is_file():
        spec = importlib.util.spec_from_file_location(
            alias, init_file, submodule_search_locations=[str(folder)]
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载 Bot 包: {folder}")
        package = importlib.util.module_from_spec(spec)
        sys.modules[alias] = package
        spec.loader.exec_module(package)
    else:
        package = types.ModuleType(alias)
        package.__path__ = [str(folder)]
        package.__package__ = alias
        sys.modules[alias] = package
    module = importlib.import_module(alias + "." + source.stem)
    from sc2.bot_ai import BotAI
    bot_type = getattr(module, "CompetitiveBot", None)
    if not inspect.isclass(bot_type) or not issubclass(bot_type, BotAI):
        raise TypeError("脚本没有导出继承 BotAI 的 CompetitiveBot: " + str(source))
    return bot_type

def create_instance(bot_type, seed: int, telemetry: Path):
    parameters = inspect.signature(bot_type).parameters
    kwargs = {}
    if "seed" in parameters:
        kwargs["seed"] = seed
    if "telemetry_path" in parameters:
        kwargs["telemetry_path"] = telemetry
    return bot_type(**kwargs)

def write_result(path: Path, value: dict):
    target = path.with_suffix(".tmp")
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    target.replace(path)

def inspect_only(config):
    from sc2 import maps
    from sc2.paths import Paths
    print("== 静态验证：不会启动 SC2 ==", flush=True)
    print("SC2PATH:", os.getenv("SC2PATH"), flush=True)
    executable = Path(Paths.EXECUTABLE)
    if not executable.is_file():
        raise FileNotFoundError(f"游戏客户端不存在：{executable}")
    game_map = maps.get(config["map"])
    if not game_map.data:
        raise ValueError("地图数据为空")
    print("客户端:", executable, flush=True)
    print("地图:", game_map.path, "字节:", len(game_map.data), flush=True)
    left = load_bot(config["left"], "arena_left")
    print("A:", left.__module__, left.__name__, flush=True)
    if config["opponent"] == "bot":
        right = load_bot(config["right"], "arena_right")
        print("B:", right.__module__, right.__name__, flush=True)
    else:
        print("B:", config["opponent"], flush=True)
    print("PASS：Python 类和本地文件已检查，尚未测试客户端启动/实际战术。", flush=True)

def actual_match(config):
    from sc2 import maps
    from sc2.data import Difficulty, Race
    from sc2.main import run_game
    from sc2.player import Bot, Computer, Human

    output_path = Path(config["result_path"])
    replay_path = Path(config["replay_path"])
    report = {"status": "STARTING", "started_at": timestamp(),
              "configuration": config, "results": [], "errors": [],
              "replay_path": str(replay_path), "replay_saved": False}
    start = time.monotonic()
    write_result(output_path, report)
    left = right = None
    try:
        left_type = load_bot(config["left"], "arena_left")
        left = create_instance(left_type, config["seed"], output_path.parent / "left_telemetry.jsonl")
        players = [Bot(Race.Terran, left, name="A: " + config["left"]["agent"])]
        print("A Bot:", config["left"]["label"], flush=True)
        if config["opponent"] == "bot":
            right_type = load_bot(config["right"], "arena_right")
            right = create_instance(right_type, (config["seed"] + 1) % 2**32,
                                    output_path.parent / "right_telemetry.jsonl")
            players.append(Bot(Race.Terran, right, name="B: " + config["right"]["agent"]))
            print("B Bot:", config["right"]["label"], flush=True)
        elif config["opponent"] == "human":
            players.append(Human(getattr(Race, config["race"]), name="Human"))
            print("B: 同机真人玩家", flush=True)
        else:
            players.append(Computer(getattr(Race, config["race"]),
                                    getattr(Difficulty, config["difficulty"])))
            print("B: 电脑", config["race"], config["difficulty"], flush=True)
        game_map = maps.get(config["map"])
        print("客户端目录:", os.environ["SC2PATH"], flush=True)
        print("地图:", game_map.path, flush=True)
        print("开始实际比赛。非实时模式可能加速，取决于电脑性能。", flush=True)
        report["status"] = "RUNNING"
        write_result(output_path, report)
        raw_result = run_game(game_map, players,
                              realtime=config["realtime"],
                              save_replay_as=str(replay_path),
                              game_time_limit=config["duration"],
                              random_seed=config["seed"], disable_fog=False)
        returned = raw_result if isinstance(raw_result, list) else [raw_result]
        report["results"] = [getattr(r, "name", str(r)) for r in returned]
        print("SC2 返回结果:", report["results"], flush=True)
        if not report["results"] or any(r not in {"Victory", "Defeat", "Tie"} for r in report["results"]):
            raise RuntimeError("客户端未返回有效终局结果。")
        if config["opponent"] == "bot" and (
            len(report["results"]) != 2 or
            set(report["results"]) not in ({"Victory", "Defeat"}, {"Tie"})
        ):
            raise RuntimeError("双 Bot 的终局结果数量或胜负关系不一致。")
        if config["opponent"] == "human" and len(report["results"]) != 2:
            raise RuntimeError("同机真人对局未取得双方完整结果。")
        for bot in (left, right):
            if bot is not None and getattr(bot, "match_error", None):
                raise RuntimeError("Bot 内部报错: " + str(bot.match_error))
        report["status"] = "TIED" if "Tie" in report["results"] else "FINISHED"
    except BaseException as exc:
        report["status"] = "FAILED"
        report["errors"].append(f"{type(exc).__name__}: {exc}")
        print("失败:", traceback.format_exc(), flush=True)
    finally:
        report["finished_at"] = timestamp()
        report["elapsed_wall_seconds"] = round(time.monotonic() - start, 2)
        report["replay_saved"] = replay_path.is_file() and replay_path.stat().st_size > 0
        report["replay_bytes"] = replay_path.stat().st_size if replay_path.is_file() else 0
        if report["status"] in {"FINISHED", "TIED"} and not report["replay_saved"]:
            report["errors"].append("取得比赛结果，但没有保存非空 Replay。")
            report["status"] = "INCOMPLETE"
        write_result(output_path, report)
        print("最终状态:", report["status"], "回放:", report["replay_saved"], flush=True)
    return 0 if report["status"] in {"FINISHED", "TIED"} else 1

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    os.environ["SC2PATH"] = config["sc2_path"]
    print("Arena worker", timestamp(), flush=True)
    if args.inspect:
        inspect_only(config)
        return 0
    return actual_match(config)

if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception:
        traceback.print_exc()
        exit_code = 1
    sys.exit(exit_code)
