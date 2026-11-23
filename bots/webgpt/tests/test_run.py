"""启动器离线测试：所有游戏入口都是假的，不会启动 SC2。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from enum import Enum
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from bots.webgpt import run


class Result(Enum):
    Victory = 1
    Defeat = 2
    Tie = 3
    Undecided = 4


class FakeBotAI:
    pass


class FakeBot(FakeBotAI):
    def __init__(self, seed: int = 0, telemetry_path: Path | None = None):
        self.seed = seed
        self.telemetry_path = telemetry_path
        self.match_error = None
        self.last_game_time = 120.0
        self.game_result = None


class FakePlayer:
    def __init__(self, race, ai=None, name=None):
        self.race = race
        self.ai = ai
        self.name = name


@pytest.fixture
def backend(tmp_path, monkeypatch):
    root = tmp_path / "SC2"
    executable = root / "Versions" / "Base98310" / "SC2_x64.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"not executable; offline test fixture")
    maps_dir = root / "Maps"
    maps_dir.mkdir()
    map_path = maps_dir / "RedCityLE.SC2Map"
    map_path.write_bytes(b"offline fake map")
    game_map = SimpleNamespace(path=map_path, data=map_path.read_bytes())
    calls = []

    def fake_run_game(selected_map, players, **kwargs):
        calls.append((selected_map, players, kwargs))
        Path(kwargs["save_replay_as"]).write_bytes(b"offline fake replay")
        return Result.Victory

    api = SimpleNamespace(
        Paths=SimpleNamespace(BASE=root, EXECUTABLE=executable, MAPS=maps_dir),
        maps=SimpleNamespace(get=lambda name: game_map),
        BotAI=FakeBotAI,
        Bot=FakePlayer,
        Human=FakePlayer,
        Computer=FakePlayer,
        Race=SimpleNamespace(**{name: name for name in run.RACES.values()}),
        Difficulty=SimpleNamespace(**{name: name for name in run.DIFFICULTIES.values()}),
        run_game=fake_run_game,
        calls=calls,
    )
    monkeypatch.setattr(run, "_installed_versions", lambda: {"burnysc2": "7.3.0", "sc2": None})
    monkeypatch.setattr(run, "_load_backend", lambda: api)
    monkeypatch.setattr(run, "_own_bot_class", lambda: FakeBot)
    return api


def report_in(output: Path):
    files = list(output.glob("*.json"))
    assert len(files) == 1
    return json.loads(files[0].read_text(encoding="utf-8"))


def test_defaults_and_unlimited():
    args = run.build_parser().parse_args([])
    assert args.map == "RedCityLE"
    assert args.game_time_limit == 1800
    assert args.opponent == "computer"
    assert not args.realtime
    assert run.build_parser().parse_args(["--game-time-limit", "0"]).game_time_limit == 0


@pytest.mark.parametrize(
    "arguments",
    [["--seed", "-1"], ["--seed", "4294967296"], ["--game-time-limit", "-1"]],
)
def test_invalid_numeric_arguments(arguments):
    with pytest.raises(SystemExit):
        run.build_parser().parse_args(arguments)


def test_help_works_without_site_packages():
    # -S 禁用 site-packages；如果帮助路径意外导入 sc2，此测试会失败。
    result = subprocess.run(
        [sys.executable, "-S", "-m", "bots.webgpt.run", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert "--check" in result.stdout


@pytest.mark.parametrize(
    ("arguments", "expected_code", "stream", "chinese_text"),
    [
        (["--help"], 0, "stdout", "人族 Bot"),
        (["--seed", "-1"], 2, "stderr", "必须是非负整数"),
    ],
)
def test_cli_chinese_output_with_legacy_pipe_encoding(
    arguments, expected_code, stream, chinese_text
):
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "cp1252"
    result = subprocess.run(
        [sys.executable, "-S", "-m", "bots.webgpt.run", *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        timeout=10,
        check=False,
    )
    assert result.returncode == expected_code
    assert chinese_text in getattr(result, stream)
    assert "UnicodeEncodeError" not in result.stderr


def test_main_preserves_stringio_capture_streams(monkeypatch):
    stdout, stderr = StringIO(), StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    with pytest.raises(SystemExit) as stopped:
        run.main(["--help"])
    assert stopped.value.code == 0
    assert sys.stdout is stdout and sys.stderr is stderr
    assert "人族 Bot" in stdout.getvalue()
    assert not stdout.closed and not stderr.closed


def test_check_never_constructs_bot_or_calls_game(backend, monkeypatch, capsys):
    def forbidden():
        raise AssertionError("--check must not instantiate a bot")

    monkeypatch.setattr(run, "_own_bot_class", forbidden)
    assert run.main(["--check", "--opponent-bot", "uninstalled.module:CompetitiveBot"]) == 0
    assert not backend.calls
    assert "没有启动 SC2" in capsys.readouterr().out


def test_missing_client_is_reported_without_start(backend, capsys):
    backend.Paths.EXECUTABLE.unlink()
    assert run.main(["--check"]) == 2
    assert "可执行文件不存在" in capsys.readouterr().err
    assert not backend.calls


def test_library_system_exit_becomes_configuration_error(backend, capsys):
    class MissingPaths:
        @property
        def EXECUTABLE(self):
            raise SystemExit(1)

    backend.Paths = MissingPaths()
    assert run.main(["--check"]) == 2
    assert "--sc2-path" in capsys.readouterr().err


def test_old_sc2_distribution_rejected(backend, monkeypatch):
    monkeypatch.setattr(run, "_installed_versions", lambda: {"burnysc2": "7.3.0", "sc2": "0.11.2"})
    assert run.main(["--check"]) == 2
    assert not backend.calls


def test_computer_match_preserves_fog_and_writes_real_artifact_metadata(backend, tmp_path):
    output = tmp_path / "out"
    assert run.main(["--output-dir", str(output), "--game-time-limit", "0"]) == 0
    data = report_in(output)
    assert data["status"] == "FINISHED"
    assert data["players"][0]["result"] == "Victory"
    assert "result" not in data["players"][1]  # 内置电脑没有单独返回结果，不能捏造。
    assert data["replay"]["saved"]
    assert Path(data["replay"]["path"]).is_file()
    assert data["environment"]["client_build_directory"] == "Base98310"
    kwargs = backend.calls[0][2]
    assert kwargs["disable_fog"] is False
    assert kwargs["game_time_limit"] is None
    assert kwargs["random_seed"] == 1


def test_mirror_swap_keeps_result_attached_to_correct_bot(backend, tmp_path):
    def play(game_map, players, **kwargs):
        assert players[0].name == "webGPT-mirror"
        assert players[0].ai.seed == 8
        assert players[1].ai.seed == 7
        assert players[0].ai.telemetry_path != players[1].ai.telemetry_path
        Path(kwargs["save_replay_as"]).write_bytes(b"offline fake replay")
        return [Result.Defeat, Result.Victory]

    backend.run_game = play
    output = tmp_path / "out"
    assert (
        run.main(
            [
                "--opponent",
                "mirror",
                "--swap",
                "--seed",
                "7",
                "--output-dir",
                str(output),
            ]
        )
        == 0
    )
    records = report_in(output)["players"]
    assert [(p["name"], p["result"]) for p in records] == [
        ("webGPT-mirror", "Defeat"),
        ("webGPT", "Victory"),
    ]


def test_computer_swap_rejected_before_start(backend, capsys):
    assert run.main(["--swap"]) == 2
    assert "不能对内置电脑" in capsys.readouterr().err
    assert not backend.calls


def test_human_mode_automatically_realtime(backend, tmp_path):
    def play(game_map, players, **kwargs):
        assert kwargs["realtime"] is True
        Path(kwargs["save_replay_as"]).write_bytes(b"offline fake replay")
        return [Result.Defeat, Result.Victory]

    backend.run_game = play
    assert run.main(["--opponent", "human", "--output-dir", str(tmp_path / "out")]) == 0


def test_launch_failure_is_error_without_replay(backend, tmp_path):
    def fail(*args, **kwargs):
        raise RuntimeError("graphics initialization failed")

    backend.run_game = fail
    output = tmp_path / "out"
    assert run.main(["--output-dir", str(output)]) == 1
    data = report_in(output)
    assert data["status"] == "ERROR"
    assert not data["replay"]["saved"]
    assert "graphics initialization failed" in data["errors"][0]
    assert all("result" not in p for p in data["players"])


def test_swallowed_bot_error_overrides_defeat_result(backend, tmp_path):
    def play(game_map, players, **kwargs):
        players[0].ai.match_error = "on_step raised KeyError"
        Path(kwargs["save_replay_as"]).write_bytes(b"offline fake replay")
        return Result.Defeat

    backend.run_game = play
    output = tmp_path / "out"
    assert run.main(["--output-dir", str(output)]) == 1
    data = report_in(output)
    assert data["status"] == "ERROR"
    assert data["players"][0]["result"] == "Defeat"  # 保留API原始结果，但不能当成正常负绩。
    assert data["players"][0]["bot_error"] == "on_step raised KeyError"


def test_missing_replay_returns_incomplete_artifact_exit_code(backend, tmp_path):
    backend.run_game = lambda *args, **kwargs: Result.Victory
    output = tmp_path / "out"
    assert run.main(["--output-dir", str(output)]) == 3
    data = report_in(output)
    assert data["status"] == "FINISHED"
    assert not data["replay"]["saved"]
    assert data["warnings"]


def test_tie_is_not_called_a_win_or_proven_timeout(backend, tmp_path):
    def play(game_map, players, **kwargs):
        Path(kwargs["save_replay_as"]).write_bytes(b"offline fake replay")
        return Result.Tie

    backend.run_game = play
    output = tmp_path / "out"
    assert run.main(["--output-dir", str(output)]) == 0
    data = report_in(output)
    assert data["status"] == "TIED"
    assert data["termination_reason"] == "tie_or_game_time_limit"
    assert "winner" not in data


@pytest.mark.parametrize("invalid", [None, Result.Undecided, []])
def test_incomplete_results_are_errors(backend, tmp_path, invalid):
    backend.run_game = lambda *args, **kwargs: invalid
    output = tmp_path / "out"
    assert run.main(["--output-dir", str(output)]) == 1
    assert report_in(output)["status"] == "ERROR"


def test_conflicting_client_results_are_not_a_valid_match(backend, tmp_path):
    backend.run_game = lambda *args, **kwargs: [Result.Victory, Result.Victory]
    output = tmp_path / "out"
    assert run.main(["--opponent", "mirror", "--output-dir", str(output)]) == 1
    data = report_in(output)
    assert data["status"] == "ERROR"
    assert all(p["result"] == "Victory" for p in data["players"])


def test_external_module_is_imported_only_for_an_explicit_match(backend, tmp_path, monkeypatch):
    imported = []

    def fake_import(name):
        imported.append(name)
        return SimpleNamespace(CompetitiveBot=FakeBot)

    def play(game_map, players, **kwargs):
        assert players[1].ai.seed == 2
        Path(kwargs["save_replay_as"]).write_bytes(b"offline fake replay")
        return [Result.Victory, Result.Defeat]

    backend.run_game = play
    monkeypatch.setattr(run.importlib, "import_module", fake_import)
    output = tmp_path / "out"
    assert (
        run.main(
            [
                "--opponent-bot",
                "mock_opponent:CompetitiveBot",
                "--output-dir",
                str(output),
            ]
        )
        == 0
    )
    assert imported == ["mock_opponent"]
    assert report_in(output)["players"][1]["entrypoint"] == "mock_opponent:CompetitiveBot"
