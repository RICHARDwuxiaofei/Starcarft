"""Run one local matchup using published entrant entry points."""

import argparse
import asyncio
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from subprocess import BELOW_NORMAL_PRIORITY_CLASS, STARTF_USESHOWWINDOW, STARTUPINFO, Popen
from typing import ClassVar
from unittest.mock import patch

from loguru import logger
from sc2 import main, maps
from sc2.bot_ai import BotAI
from sc2.data import Race, Result
from sc2.player import Bot
from sc2.sc2process import SC2Process

ENTRANTS = {
    "mizuki": (
        Path(os.environ.get("SC2_MIZUKI_ROOT", "../Starcarft-agent-worktrees/mizuki-arena")),
        "bots.mizuki.bot",
        Race.Terran,
    ),
    "webgpt": (
        Path(os.environ.get("SC2_WEBGPT_ROOT", "../Starcarft-agent-worktrees/webGPT-arena")),
        "bots.webgpt.bot",
        Race.Terran,
    ),
}


class ClientProcess(SC2Process):
    launched: ClassVar[list[Popen]] = []
    compact_fps: ClassVar[int | None] = None

    def __init__(self, **kwargs):
        if self.compact_fps:
            kwargs["resolution"] = (640, 480)
            kwargs["placement"] = (12 + 668 * len(self.launched), 52)
        super().__init__(**kwargs)

    def _launch(self):
        if self.compact_fps:
            def launch_compact(args, **kwargs):
                startup = STARTUPINFO(dwFlags=STARTF_USESHOWWINDOW, wShowWindow=4)  # SW_SHOWNOACTIVATE
                return Popen(
                    [*args, f"-frameratecap={self.compact_fps}", f"-frameratecapglue={self.compact_fps}"],
                    startupinfo=startup,
                    creationflags=BELOW_NORMAL_PRIORITY_CLASS,
                    **kwargs,
                )

            with patch("sc2.sc2process.subprocess.Popen", launch_compact):
                process = super()._launch()
        else:
            process = super()._launch()
        self.launched.append(process)
        logger.info("SC2 launched pid={} port={} args={}", process.pid, self._port, process.args)
        return process

    async def __aexit__(self, *args):
        # Each client owns its process. The SDK's global kill switch can
        # otherwise end the second client while it handles its final result.
        try:
            await self._close_connection()
        finally:
            self._clean()


def load_bot(name, example):
    if name == "burnysc2":
        spec = importlib.util.spec_from_file_location("burny_zerg_rush", example)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.ZergRushBot(), Race.Zerg

    root, module_name, race = ENTRANTS[name]
    sys.path.insert(0, str(root))
    try:
        # The two published worktrees each own a separate top-level bots namespace.
        for key in [key for key in sys.modules if key == "bots" or key.startswith("bots.")]:
            del sys.modules[key]
        return importlib.import_module(module_name).CompetitiveBot(), race
    finally:
        sys.path.remove(str(root))


def instrument(bot, label, record, end_barrier, checkpoint=None, checkpoint_at=None, recovery=None):
    start = bot.on_start
    step = bot.on_step
    end = bot.on_end

    async def on_start():
        await start()
        record["player_id"] = bot.player_id
        logger.info("{} joined player {}", label, bot.player_id)

    async def on_step(iteration):
        nonlocal checkpoint
        if checkpoint is not None and time.monotonic() >= checkpoint_at:
            await bot.client.save_replay(str(checkpoint))
            checkpoint = None
        await step(iteration)
        record["steps"] += 1
        record["game_seconds"] = round(bot.time, 2)
        record["workers"] = bot.workers.amount
        record["units"] = bot.units.amount
        losses = bot.state.dead_units
        record["death_events_observed"] += len(losses)
        if bot.enemy_units.closer_than(24, bot.start_location) and not record.get("enemy_at_home"):
            record["enemy_at_home"] = record["game_seconds"]
            logger.info("{} saw enemy units near its starting base at {}s", label, bot.time_formatted)
        if iteration == 0 or bot.time >= record["next_report"]:
            logger.info("{} time={} workers={} units={} enemy_visible={}", label,
                        bot.time_formatted, bot.workers.amount, bot.units.amount, bot.enemy_units.amount)
            record["next_report"] = bot.time + 60

    async def on_end(result):
        # In burnysc2 7.3.0 the game-clock limit returns Tie without an
        # authoritative result in the latest observation.
        at_game_limit = result is Result.Tie and not bot.client._game_result
        await end(result)
        record["result"] = result.name
        logger.info("{} result={} last_game_seconds={}", label, result.name, record["game_seconds"])
        if recovery is None:
            await asyncio.wait_for(end_barrier.wait(), timeout=10)
        else:
            await recovery.after_result(bot.client, label, at_game_limit=at_game_limit)

    bot.on_start = on_start
    bot.on_step = on_step
    bot.on_end = on_end


class DisconnectRecovery:
    """Retain the first client-side failure surfaced before run_game aggregates results."""

    def __init__(self, barrier, replay, game_limit_replay):
        self.barrier = barrier
        self.replay = replay
        self.game_limit_replay = game_limit_replay
        self.game_limit_by = None
        self.first_failure = None
        self.save_lock = asyncio.Lock()

    def record_client_failure(self, label, exc):
        if self.first_failure is None:
            self.first_failure = {
                "player": label,
                "type": type(exc).__name__,
                "message": str(exc),
                "time": datetime.now().astimezone().isoformat(),
            }
            logger.error("First SDK client failure: {}", self.first_failure)

    async def after_result(self, client, label, *, at_game_limit=False):
        if at_game_limit and self.game_limit_by is None and self.first_failure is None:
            self.game_limit_by = label
            # The peer can still be blocked in Step until this client leaves.
            # Save from the client that reached the limit before releasing it.
            async with self.save_lock:
                await self.barrier.abort()
                await self.save_partial(client, self.game_limit_replay)
            return

        if self.game_limit_by is None and self.first_failure is None:
            try:
                await asyncio.wait_for(self.barrier.wait(), timeout=10)
                return
            except (TimeoutError, asyncio.BrokenBarrierError) as exc:
                if self.game_limit_by is None:
                    self.record_client_failure(label, exc)

        async with self.save_lock:
            if self.first_failure is not None:
                await self.save_partial(client, self.replay)

    async def save_partial(self, client, path):
        if path.is_file():
            return
        try:
            await asyncio.wait_for(client.save_replay(str(path)), timeout=4)
        except Exception as exc:  # noqa: BLE001 - dead connection or failed replay request
            logger.warning("Partial replay unavailable at {}: {}", path, exc)


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("first")
    parser.add_argument("second")
    parser.add_argument("example", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("deadline")
    parser.add_argument("--natural", action="store_true", help="Let SC2 decide the result")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    logger.remove()
    logger.add(sys.stdout, level="INFO")
    logger.add(args.output / "console.log", level="INFO")

    names = [args.first, args.second]
    records = {
        name: {"steps": 0, "game_seconds": 0, "next_report": 0, "death_events_observed": 0}
        for name in names
    }
    start_time = time.monotonic()
    limit = (datetime.fromisoformat(args.deadline) - datetime.now()).total_seconds()
    if not args.natural:
        limit = min(125, limit)
    if limit < 15:
        raise RuntimeError("Too little time remains for another match")

    def close_owned_clients():
        for process in ClientProcess.launched:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

    replay = args.output / "match.SC2Replay"
    checkpoint = args.output / "wall-limit-checkpoint.SC2Replay"
    disconnected_replay = args.output / "disconnect-checkpoint.SC2Replay"
    game_limit_replay = args.output / "game-limit-checkpoint.SC2Replay"
    recovery = DisconnectRecovery(asyncio.Barrier(2), disconnected_replay, game_limit_replay)

    def write_outcome(error):
        game_limited = recovery.game_limit_by is not None
        if game_limited:
            # SDK replay writes after leave can include a synthetic victory.
            candidates = (game_limit_replay, checkpoint)
        elif error:
            candidates = (disconnected_replay, replay, checkpoint)
        else:
            candidates = (replay, checkpoint)
        saved = next((path for path in candidates if path.is_file()), None)
        if error or game_limited:
            # Keep callbacks from exceptional or artificial endings as diagnostic data.
            for record in records.values():
                if "result" in record:
                    record["reported_result"] = record.pop("result")
        outcome = {
            "players": records, "error": error,
            "termination": error or ("game_time_limit" if game_limited else "natural"),
            "game_limit_by": recovery.game_limit_by,
            "wall_seconds": round(time.monotonic() - start_time, 2),
            "pids": [process.pid for process in ClientProcess.launched],
            "first_sdk_client_failure": recovery.first_failure,
            "replay_kind": (
                "partial" if error or game_limited or saved != replay else "complete"
            ) if saved else None,
            "replay": saved.name if saved else None,
            "replay_bytes": saved.stat().st_size if saved else 0,
        }
        (args.output / "result.json").write_text(json.dumps(outcome, indent=2), encoding="utf-8")
        logger.info("{}", outcome)
        if saved:
            tool = Path(os.environ.get("SC2_SCORE_TOOL", "tools/score_replay.py"))
            env = os.environ.copy()
            try:
                proc = subprocess.run(
                    [sys.executable, str(tool), str(saved), "--players", *names,
                     "--out", str(args.output / "score.json")],
                    env=env, capture_output=True, text=True, timeout=45, check=False,
                )
                if proc.returncode:
                    logger.warning("Replay scoring failed: {}", proc.stderr.strip())
            except (OSError, subprocess.TimeoutExpired) as exc:
                logger.warning("Replay scoring failed: {}", exc)

    def timeout():
        logger.error("Wall-clock limit reached for {}", names)
        try:
            close_owned_clients()
        finally:
            write_outcome("wall_timeout")
            os._exit(124)

    timer = threading.Timer(limit, timeout)
    timer.daemon = True
    timer.start()

    error = None
    try:
        players = []
        barrier = recovery.barrier
        for index, name in enumerate(names):
            ai, race = load_bot(name, args.example)
            assert isinstance(ai, BotAI)
            partial_replay = checkpoint if args.natural and index == 0 else None
            instrument(ai, name, records[name], barrier, partial_replay, start_time + limit - 15, recovery)
            players.append(Bot(race, ai, name=name))
        ClientProcess.compact_fps = 30
        main.SC2Process = ClientProcess
        join = main._join_game

        async def join_without_replay(*join_args, **kwargs):
            kwargs["save_replay_as"] = None
            return await join(*join_args, **kwargs)

        main._join_game = join_without_replay
        play_game = main._play_game

        async def play_with_failure_record(player, client, *play_args, **play_kwargs):
            try:
                return await play_game(player, client, *play_args, **play_kwargs)
            except Exception as exc:
                recovery.record_client_failure(player.name, exc)
                raise

        main._play_game = play_with_failure_record
        logger.info("Starting {} vs {} on RedCityLE in accelerated mode", *names)
        main.run_game(
            maps.get("RedCityLE"), players, realtime=False,
            game_time_limit=None if args.natural else 360,
            random_seed=98746, save_replay_as=str(replay),
        )
    except Exception as exc:  # noqa: BLE001 - isolate arbitrary entrant failures
        logger.exception("Match failed")
        error = str(exc) or type(exc).__name__
    finally:
        close_owned_clients()
        timer.cancel()

    error = error or ("client_failure" if recovery.first_failure else None)
    write_outcome(error)
    return int(error is not None)


if __name__ == "__main__":
    sys.exit(run())
