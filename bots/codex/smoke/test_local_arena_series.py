"""Offline regression coverage for accelerated arena shutdown."""

import asyncio
import json
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

import local_arena_series as arena


class ReplayClient:
    def __init__(self):
        self.requests = []

    async def save_replay(self, filename):
        self.requests.append(Path(filename).name)
        Path(filename).write_bytes(b"replay")


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.recovery = arena.DisconnectRecovery(
            asyncio.Barrier(2),
            self.root / "disconnect.SC2Replay",
            self.root / "limit.SC2Replay",
        )

    async def test_cutoff_releases_peer_blocked_in_step(self):
        leader = ReplayClient()
        peer = ReplayClient()
        left = asyncio.Event()

        async def blocked_peer():
            await left.wait()
            await self.recovery.after_result(peer, "peer")

        async def cutoff_client():
            await self.recovery.after_result(leader, "leader", at_game_limit=True)
            self.assertEqual(self.recovery.game_limit_replay.read_bytes(), b"replay")
            left.set()

        await asyncio.wait_for(asyncio.gather(blocked_peer(), cutoff_client()), timeout=1)
        self.assertIsNone(self.recovery.first_failure)
        self.assertEqual(self.recovery.game_limit_by, "leader")
        self.assertEqual(leader.requests, ["limit.SC2Replay"])
        self.assertEqual(peer.requests, [])

    async def test_cutoff_releases_peer_already_in_callback(self):
        peer = asyncio.create_task(self.recovery.after_result(ReplayClient(), "peer"))
        while self.recovery.barrier.n_waiting == 0:
            await asyncio.sleep(0)
        await self.recovery.after_result(ReplayClient(), "leader", at_game_limit=True)
        await asyncio.wait_for(peer, timeout=1)
        self.assertIsNone(self.recovery.first_failure)

    async def test_natural_results_do_not_save_a_partial_replay(self):
        first, second = ReplayClient(), ReplayClient()
        await asyncio.wait_for(asyncio.gather(
            self.recovery.after_result(first, "first"),
            self.recovery.after_result(second, "second"),
        ), timeout=1)
        self.assertIsNone(self.recovery.first_failure)
        self.assertIsNone(self.recovery.game_limit_by)
        self.assertEqual(first.requests + second.requests, [])

    async def test_failure_precedes_later_cutoff_callback(self):
        self.recovery.record_client_failure("peer", ConnectionError("connection lost"))
        client = ReplayClient()
        await self.recovery.after_result(client, "leader", at_game_limit=True)
        self.assertEqual(self.recovery.first_failure["player"], "peer")
        self.assertIsNone(self.recovery.game_limit_by)
        self.assertEqual(client.requests, ["disconnect.SC2Replay"])

    async def test_unavailable_checkpoint_still_releases_peer(self):
        client = ReplayClient()
        client.save_replay = AsyncMock(side_effect=OSError("could not save"))
        await asyncio.wait_for(
            self.recovery.after_result(client, "leader", at_game_limit=True), timeout=1
        )
        await self.recovery.after_result(ReplayClient(), "peer")
        self.assertIsNone(self.recovery.first_failure)
        self.assertFalse(self.recovery.game_limit_replay.exists())


class RunnerTests(unittest.TestCase):
    def test_cutoff_does_not_report_post_leave_victory_or_score_as_win(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "match"
            argv = ["arena", "a", "b", "unused", str(output), "2099-01-01T00:00:00"]
            players = [arena.BotAI(), arena.BotAI()]
            players[0].client = ReplayClient()
            players[0].client._game_result = None
            players[1].client = ReplayClient()
            players[1].client._game_result = {2: arena.Result.Victory}

            def run_sdk(*args, **kwargs):
                async def end_game():
                    await players[0].on_end(arena.Result.Tie)
                    await players[1].on_end(arena.Result.Victory)
                asyncio.run(end_game())
                (output / "match.SC2Replay").write_bytes(b"post-leave")

            def load_bot(name, example):
                player = players[["a", "b"].index(name)]
                return player, arena.Race.Terran

            with (
                patch.object(sys, "argv", argv),
                patch.object(threading, "Timer", return_value=MagicMock()),
                patch.object(arena, "load_bot", side_effect=load_bot),
                patch.object(arena.maps, "get"),
                patch.object(arena.subprocess, "run", return_value=MagicMock(returncode=0)) as score,
                patch.object(arena.main, "run_game", side_effect=run_sdk),
                patch.object(arena.main, "_join_game"),
                patch.object(arena.main, "_play_game"),
                patch.object(arena.main, "SC2Process"),
                patch.object(arena.logger, "remove"),
                patch.object(arena.logger, "add"),
                patch.object(arena.ClientProcess, "compact_fps", None),
            ):
                self.assertEqual(arena.run(), 0)

            outcome = json.loads((output / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(outcome["termination"], "game_time_limit")
            self.assertIsNone(outcome["error"])
            self.assertEqual(outcome["game_limit_by"], "a")
            self.assertEqual(outcome["players"]["b"]["reported_result"], "Victory")
            self.assertNotIn("result", outcome["players"]["b"])
            self.assertEqual(outcome["replay_kind"], "partial")
            self.assertEqual(outcome["replay"], "game-limit-checkpoint.SC2Replay")
            self.assertIn(str(output / outcome["replay"]), score.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
