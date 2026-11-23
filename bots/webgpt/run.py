"""webGPT 本地比赛入口；导入本文件和查看帮助都不会启动 SC2。

运行示例：
    python -m bots.webgpt.run --check
    python -m bots.webgpt.run --opponent computer --difficulty medium
    python -m bots.webgpt.run --opponent mirror --realtime

本文件只负责比赛配置和如实记录结果，不包含对手实现，也不会下载地图。
"""

from __future__ import annotations

import argparse
import codecs
import hashlib
import importlib
import importlib.metadata
import inspect
import json
import os
import platform
import sys
import time
import traceback
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REQUIRED_BURNYSC2 = "7.3.0"
DIFFICULTIES = {
    "very-easy": "VeryEasy",
    "easy": "Easy",
    "medium": "Medium",
    "medium-hard": "MediumHard",
    "hard": "Hard",
    "harder": "Harder",
    "very-hard": "VeryHard",
}
RACES = {name.lower(): name for name in ("Terran", "Protoss", "Zerg", "Random")}
FINAL_RESULTS = {"Victory", "Defeat", "Tie"}


class ConfigurationError(RuntimeError):
    """可在启动游戏前解释给用户的环境或参数问题。"""


def _configure_utf8_output() -> None:
    """CLI 文本统一使用 UTF-8，包括 Windows 重定向到管道后的中文帮助。

    只调整支持 reconfigure 的实际文本流；pytest、StringIO 和其他嵌入式
    调用者可能提供不可重设的捕获流，这时保留调用者的流，不替换或关闭它。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        encoding = getattr(stream, "encoding", None)
        if not callable(reconfigure) or not isinstance(encoding, str):
            continue
        try:
            if codecs.lookup(encoding).name == "utf-8":
                continue
        except LookupError:
            pass
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            # 例如被宿主锁定的文本流：不因不能改变捕获方式而中止检查/比赛。
            continue


def _nonnegative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("必须是非负整数。")
    return number


def _seed(value: str) -> int:
    number = _nonnegative(value)
    if number > 2**32 - 1:
        raise argparse.ArgumentTypeError("随机种子必须在 0 到 4294967295 之间。")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="webGPT 人族 Bot：检查环境或运行一场本地 SC2 比赛。",
        epilog="--check 不启动游戏。人类模式自动使用实时速度；所有模式保留战争迷雾。",
    )
    parser.add_argument("--check", action="store_true", help="只检查依赖、游戏路径和地图")
    parser.add_argument("--map", default="RedCityLE", help="Maps 中地图名，不含 .SC2Map")
    parser.add_argument("--sc2-path", type=Path, help="游戏根目录，包含 Versions 和 Maps")
    parser.add_argument("--opponent", choices=("computer", "mirror", "human"), default="computer")
    parser.add_argument("--difficulty", choices=DIFFICULTIES, default="medium")
    parser.add_argument("--opponent-race", choices=RACES, default="terran")
    parser.add_argument(
        "--opponent-bot",
        metavar="MODULE:CLASS",
        help="运行时导入用户指定的 BotAI 类；例如 bots.other.bot:CompetitiveBot",
    )
    parser.add_argument("--realtime", action="store_true", help="用实时速度运行，方便观看")
    parser.add_argument("--seed", type=_seed, default=1)
    parser.add_argument(
        "--game-time-limit",
        type=_nonnegative,
        default=1800,
        metavar="SECONDS",
        help="游戏时间上限；默认 1800 秒，0 表示不限时，不是墙钟超时",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("results/webgpt"))
    parser.add_argument(
        "--swap",
        action="store_true",
        help="交换双方加入槽位；仅 mirror、human 或 --opponent-bot 模式可用",
    )
    return parser


def validate_arguments(args: argparse.Namespace) -> None:
    if not args.map or Path(args.map).name != args.map or args.map.endswith(".SC2Map"):
        raise ConfigurationError("--map 请填写地图名，如 RedCityLE，不要填路径或 .SC2Map 后缀。")
    if args.opponent_bot:
        parts = args.opponent_bot.split(":")
        if (
            len(parts) != 2
            or not all(part.isidentifier() for part in parts[0].split("."))
            or not parts[1].isidentifier()
        ):
            raise ConfigurationError("--opponent-bot 格式必须是 Python模块:类名。")
        if args.opponent != "computer":
            raise ConfigurationError("--opponent-bot 与 --opponent mirror/human 不能同时使用。")
    elif args.swap and args.opponent == "computer":
        raise ConfigurationError(
            "burnysc2 的单客户端入口要求 Bot 在首位，不能对内置电脑使用 --swap；"
            "请在 mirror、human 或 --opponent-bot 对局中交换槽位。"
        )


def _installed_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for package in ("burnysc2", "sc2"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return versions


def _load_backend() -> SimpleNamespace:
    """延迟导入，避免 --help 依赖游戏库，也让 SC2PATH 先于 Paths 生效。"""
    from sc2 import maps
    from sc2.bot_ai import BotAI
    from sc2.data import Difficulty, Race
    from sc2.main import run_game
    from sc2.paths import Paths
    from sc2.player import Bot, Computer, Human

    return SimpleNamespace(
        maps=maps,
        BotAI=BotAI,
        Difficulty=Difficulty,
        Race=Race,
        run_game=run_game,
        Paths=Paths,
        Bot=Bot,
        Computer=Computer,
        Human=Human,
    )


def inspect_environment(args: argparse.Namespace) -> tuple[Any, Any, dict[str, Any]]:
    """只读检查；不创建 Bot，不连接 API，不启动 SC2 或 Battle.net。"""
    versions = _installed_versions()
    if versions["sc2"] is not None:
        raise ConfigurationError(
            "此环境装有废弃的 sc2 发行包；请使用只安装 burnysc2 的独立虚拟环境。"
        )
    if versions["burnysc2"] != REQUIRED_BURNYSC2:
        raise ConfigurationError(
            f"需要 burnysc2=={REQUIRED_BURNYSC2}，当前是 {versions['burnysc2'] or '未安装'}。"
            "请在项目虚拟环境执行 python -m pip install -r requirements.txt。"
        )
    if args.sc2_path is not None:
        os.environ["SC2PATH"] = str(args.sc2_path.expanduser().resolve())
    backend = _load_backend()
    try:
        executable = Path(backend.Paths.EXECUTABLE)
        root = Path(backend.Paths.BASE)
        maps_dir = Path(backend.Paths.MAPS)
    except (SystemExit, OSError, ValueError, KeyError) as exc:
        raise ConfigurationError(
            "未能定位 SC2 客户端。请安装暴雪官方游戏，并通过 --sc2-path 指定含 Versions 的根目录，"
            '例如 --sc2-path "C:\\baoxue\\StarCraft II"。检查不会启动游戏。'
        ) from exc
    if not executable.is_file():
        raise ConfigurationError(f"游戏可执行文件不存在：{executable}。请检查游戏安装是否完整。")
    if not maps_dir.is_dir():
        raise ConfigurationError(f"地图目录不存在：{maps_dir}。请先准备 {args.map}.SC2Map。")
    try:
        game_map = backend.maps.get(args.map)
        map_bytes = game_map.data
    except (KeyError, OSError) as exc:
        raise ConfigurationError(f"无法读取 {maps_dir} 中的 {args.map}.SC2Map：{exc}") from exc
    if not map_bytes:
        raise ConfigurationError("地图文件为空；请重新取得完整的 .SC2Map 文件。")
    return (
        backend,
        game_map,
        {
            "python": platform.python_version(),
            "burnysc2": versions["burnysc2"],
            "platform": platform.system(),
            "sc2_root": str(root),
            "sc2_executable": str(executable),
            "client_build_directory": executable.parent.name,
            "map_path": str(game_map.path),
            "map_bytes": len(map_bytes),
            "map_sha256": hashlib.sha256(map_bytes).hexdigest(),
            "validation": "file_and_import_checks_only_not_a_played_game",
        },
    )


def _own_bot_class() -> Any:
    from .bot import CompetitiveBot

    return CompetitiveBot


def make_players(args: argparse.Namespace, backend: Any, output: Path, run_id: str):
    """只在真的请求比赛时构造 Bot；外部类也只在这里导入。"""
    own_class = _own_bot_class()
    own = own_class(seed=args.seed, telemetry_path=output / f"{run_id}_webgpt.jsonl")
    records = [{"name": "webGPT", "kind": "bot", "race": "Terran", "seed": args.seed}]
    players = [backend.Bot(backend.Race.Terran, own, name="webGPT")]
    bots: dict[str, Any] = {"webGPT": own}
    other_seed = (args.seed + 1) % 2**32
    opponent_race = getattr(backend.Race, RACES[args.opponent_race])
    if args.opponent_bot:
        module_name, class_name = args.opponent_bot.split(":")
        candidate = getattr(importlib.import_module(module_name), class_name)
        if not inspect.isclass(candidate) or not issubclass(candidate, backend.BotAI):
            raise ConfigurationError("--opponent-bot 必须导出 BotAI 的子类。")
        # 公共接口允许无参数构造；只有显式声明了这些参数才传递它们。
        parameters = inspect.signature(candidate).parameters
        kwargs: dict[str, Any] = {}
        if "seed" in parameters:
            kwargs["seed"] = other_seed
        if "telemetry_path" in parameters:
            kwargs["telemetry_path"] = output / f"{run_id}_opponent.jsonl"
        other = candidate(**kwargs)
        bots["external"] = other
        players.append(backend.Bot(opponent_race, other, name="external"))
        records.append(
            {
                "name": "external",
                "kind": "bot",
                "race": RACES[args.opponent_race],
                "entrypoint": args.opponent_bot,
                "seed": kwargs.get("seed"),
            }
        )
    elif args.opponent == "mirror":
        other = own_class(seed=other_seed, telemetry_path=output / f"{run_id}_mirror.jsonl")
        bots["webGPT-mirror"] = other
        players.append(backend.Bot(backend.Race.Terran, other, name="webGPT-mirror"))
        records.append(
            {
                "name": "webGPT-mirror",
                "kind": "bot",
                "race": "Terran",
                "seed": other_seed,
            }
        )
    elif args.opponent == "human":
        players.append(backend.Human(opponent_race, name="Human"))
        records.append({"name": "Human", "kind": "human", "race": RACES[args.opponent_race]})
    else:
        difficulty = getattr(backend.Difficulty, DIFFICULTIES[args.difficulty])
        players.append(backend.Computer(opponent_race, difficulty))
        records.append(
            {
                "name": "Computer",
                "kind": "computer",
                "race": RACES[args.opponent_race],
                "difficulty": DIFFICULTIES[args.difficulty],
            }
        )
    if args.swap:
        players.reverse()
        records.reverse()
    return players, records, bots


def _write_report(path: Path, report: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _result_name(value: Any) -> str | None:
    if value is None:
        return None
    return getattr(value, "name", str(value).removeprefix("Result."))


def run_match(
    args: argparse.Namespace, backend: Any, game_map: Any, environment: dict[str, Any]
) -> int:
    output = args.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ") + "_" + uuid.uuid4().hex[:8]
    report_path = output / f"{run_id}.json"
    replay = output / f"{run_id}.SC2Replay"
    report: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "status": "STARTING",
        "started_at_utc": _utc_now(),
        "environment": environment,
        "configuration": {
            "map": args.map,
            "seed": args.seed,
            "realtime": args.realtime or args.opponent == "human",
            "game_time_limit_seconds": args.game_time_limit or None,
            "swap_player_slots": args.swap,
            "disable_fog": False,
        },
        "players": [],
        "errors": [],
        "warnings": [],
        "replay": {"path": str(replay), "exists": False, "saved": False, "bytes": 0},
    }
    bots: dict[str, Any] = {}
    started = time.monotonic()
    exit_code = 1
    _write_report(report_path, report)
    try:
        players, records, bots = make_players(args, backend, output, run_id)
        report["players"] = records
        report["status"] = "RUNNING"
        _write_report(report_path, report)
        raw = backend.run_game(
            game_map,
            players,
            realtime=report["configuration"]["realtime"],
            save_replay_as=str(replay),
            game_time_limit=args.game_time_limit or None,
            random_seed=args.seed,
            disable_fog=False,
        )
        returned = raw if isinstance(raw, list) else [raw]
        participants = [p for p in records if p["kind"] != "computer"]
        if len(returned) != len(participants):
            raise RuntimeError(
                f"比赛返回 {len(returned)} 个结果，但应有 {len(participants)} 个客户端结果。"
            )
        for participant, result in zip(participants, returned, strict=True):
            participant["result"] = _result_name(result)
        if any(p["result"] not in FINAL_RESULTS for p in participants):
            raise RuntimeError("比赛没有返回完整终局结果；不会将 None/Undecided 当成胜负。")
        if len(participants) == 2 and {p["result"] for p in participants} not in (
            {"Victory", "Defeat"},
            {"Tie"},
        ):
            raise RuntimeError("双方终局结果不一致；保留原始结果，不能记为有效胜负。")
        report["status"] = "TIED" if any(p["result"] == "Tie" for p in participants) else "FINISHED"
        # 库达到时间上限会返回 Tie；不虚构胜者，也不把所有平局断言为超时。
        report["termination_reason"] = (
            "tie_or_game_time_limit" if report["status"] == "TIED" else "game_result"
        )
        exit_code = 0
    except KeyboardInterrupt:
        report["status"] = "INTERRUPTED"
        report["errors"].append("用户中断；没有据此判定任何一方获胜。")
        exit_code = 130
    except (Exception, SystemExit) as exc:  # noqa: BLE001 -- 比赛进程边界，必须记录并以非零退出。
        report["status"] = "ERROR"
        report["errors"].append(f"{type(exc).__name__}: {exc}")
        report["traceback"] = traceback.format_exc()
    finally:
        for player in report["players"]:
            bot = bots.get(player["name"])
            if bot is None:
                continue
            player["bot_error"] = getattr(bot, "match_error", None)
            player["last_game_time"] = getattr(bot, "last_game_time", None)
            player["callback_game_result"] = _result_name(getattr(bot, "game_result", None))
            if player["bot_error"]:
                report["errors"].append(f"{player['name']}: {player['bot_error']}")
                report["status"] = "ERROR"
                exit_code = 1
        exists = replay.is_file()
        size = replay.stat().st_size if exists else 0
        report["replay"].update(exists=exists, bytes=size, saved=exists and size > 0)
        if exit_code == 0 and not report["replay"]["saved"]:
            report["warnings"].append(
                "已获得游戏结果，但没有实际保存非空回放文件；请检查 SC2 日志。"
            )
            exit_code = 3
        report["elapsed_wall_seconds"] = round(time.monotonic() - started, 3)
        report["finished_at_utc"] = _utc_now()
        report["exit_code"] = exit_code
        _write_report(report_path, report)
    print(f"比赛状态：{report['status']}")
    for player in report["players"]:
        print(f"  {player['name']}: {player.get('result', '未取得单独结果')}")
    for message in report["errors"] + report["warnings"]:
        print(message, file=sys.stderr)
    print(f"报告：{report_path}")
    print(f"回放：{replay if report['replay']['saved'] else '未保存'}")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    # argparse 在解析 --help 或参数错误时就会写 stdout/stderr，必须先设编码。
    _configure_utf8_output()
    args = build_parser().parse_args(argv)
    try:
        validate_arguments(args)
        backend, game_map, environment = inspect_environment(args)
        if args.check:
            print("环境检查通过；没有启动 SC2，未验证显卡、地图加载或实战兼容性。")
            print(json.dumps(environment, ensure_ascii=False, indent=2))
            return 0
        return run_match(args, backend, game_map, environment)
    except (ConfigurationError, OSError, ImportError) as exc:
        print(f"无法开始：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
