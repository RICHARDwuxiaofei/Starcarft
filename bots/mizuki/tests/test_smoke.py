from unittest.mock import Mock

from sc2.data import Result
from sc2.player import Bot, Computer

from bots.mizuki import smoke


def test_launcher_wires_fog_result_and_replay_without_starting_sc2(
    monkeypatch, tmp_path, capsys
):
    game = Mock(return_value=Result.Victory)
    map_stub = object()
    map_factory = Mock(return_value=map_stub)
    monkeypatch.setattr(smoke, "run_game", game)
    monkeypatch.setattr(smoke.maps, "get", map_factory)
    replay = tmp_path / "trial.SC2Replay"
    assert (
        smoke.main(["--race", "Protoss", "--seed", "23", "--replay", str(replay)]) == 0
    )
    map_factory.assert_called_once_with("RedCityLE")
    arena, players = game.call_args.args
    assert arena is map_stub
    assert isinstance(players[0], Bot) and isinstance(players[1], Computer)
    assert game.call_args.kwargs["random_seed"] == 23
    assert not game.call_args.kwargs["disable_fog"]
    assert game.call_args.kwargs["save_replay_as"] == str(replay)
    assert not replay.exists()
    assert '"result": "Victory"' in capsys.readouterr().out
