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
from .trace import DecisionTrace


def _replay_stamp(path: Path | None) -> tuple[int, int, int] | None:
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


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
        "--review-json",
        type=Path,
        help="turn the saved replay into an agent-ready digest",
    )
    parser.add_argument("--realtime", action="store_true")
    args = parser.parse_args(argv)
    if args.seconds < 1:
        parser.error("--seconds must be positive")
    if args.replay is not None and not args.replay.parent.is_dir():
        parser.error("replay parent directory does not exist")

    if args.review_json is not None:
        if args.replay is None:
            parser.error("--review-json requires --replay")
        if (
            args.review_json.suffix.lower() != ".json"
            or args.review_json.resolve() == args.replay.resolve()
        ):
            parser.error("--review-json must be a .json file distinct from the replay")

    trace = DecisionTrace() if args.review_json is not None else None
    previous = _replay_stamp(args.replay) if trace is not None else None
    failure = None
    try:
        arena = maps.get(args.map)
        players = [
            Bot(Race.Terran, CompetitiveBot(trace=trace), name="Mizuki"),
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
    except Exception as exc:
        if trace is None:
            raise
        outcome = None
        failure = (
            f"SC2 launcher failed ({type(exc).__name__}); no current replay analyzed."
        )
    if outcome is None and failure is None:
        failure = "No SC2 result returned."
    if failure is not None and trace is None:
        print(failure)
        return 2
    context = {
        "bot": "mizuki",
        "result": outcome.name if outcome is not None else None,
        "map": args.map,
        "opponent": args.race,
        "difficulty": args.difficulty,
        "seed": args.seed,
        "game_time_limit_seconds": args.seconds,
        "realtime": args.realtime,
    }
    print(json.dumps(context))
    if args.review_json is not None and trace is not None:
        from .replay_review import main as review

        latest = _replay_stamp(args.replay)
        if failure is None and (latest is None or latest == previous or latest[0] == 0):
            failure = "SC2 did not write a new nonempty replay; an older replay was not reused."
        aborted = trace.last_error is not None or (
            context["result"] == "Defeat"
            and not trace.start_completed
            and trace.steps_completed == 0
        )
        status = review(
            [str(args.replay), "--out", str(args.review_json)],
            local_run={
                **context,
                "bot_execution_incomplete": aborted,
                "decision_trace": trace.export(),
            },
            skip_reason=failure,
        )
        return status or (2 if aborted else 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
