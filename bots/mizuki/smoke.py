"""Opt-in local private SC2 match. Importing this module never launches anything."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sc2 import maps
from sc2.data import Difficulty, Race
from sc2.main import run_game
from sc2.player import Bot, Computer

from .bot import CompetitiveBot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--map", default="RedCityLE", help="exact installed .SC2Map filename stem"
    )
    parser.add_argument(
        "--race", choices=("Terran", "Protoss", "Zerg"), default="Terran"
    )
    parser.add_argument(
        "--difficulty",
        choices=("Easy", "Medium", "Hard", "VeryHard", "Elite"),
        default="Medium",
    )
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument(
        "--seconds", type=int, default=1500, help="maximum game time in seconds"
    )
    parser.add_argument("--replay", type=Path)
    parser.add_argument(
        "--swap", action="store_true", help="swap player registration order"
    )
    parser.add_argument("--realtime", action="store_true")
    args = parser.parse_args(argv)
    if args.seconds < 1:
        parser.error("--seconds must be positive")
    if args.replay is not None and not args.replay.parent.is_dir():
        parser.error("replay parent directory does not exist")

    arena = maps.get(args.map)
    players = [
        Bot(Race.Terran, CompetitiveBot()),
        Computer(Race[args.race], Difficulty[args.difficulty]),
    ]
    outcome = run_game(
        arena,
        players,
        realtime=args.realtime,
        disable_fog=False,
        random_seed=args.seed,
        game_time_limit=args.seconds,
        save_replay_as=str(args.replay) if args.replay else None,
    )
    if outcome is None:
        print("No SC2 result returned.")
        return 2
    print(
        json.dumps(
            {
                "bot": "mizuki",
                "result": outcome.name,
                "map": args.map,
                "opponent": args.race,
                "difficulty": args.difficulty,
                "seed": args.seed,
                "game_time_limit_seconds": args.seconds,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
