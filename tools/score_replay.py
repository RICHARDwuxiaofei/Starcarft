"""Score a two-player SC2 replay from its last shared tracker sample."""

import argparse
import json
from importlib import resources, util
from pathlib import Path

from mpyq import MPQArchive

WORKER_POINTS = 50
SCORE_FIELDS = {
    "army_minerals": "m_scoreValueMineralsUsedCurrentArmy",
    "army_vespene": "m_scoreValueVespeneUsedCurrentArmy",
    "active_workers": "m_scoreValueWorkersActiveCount",
}


def load_decoder(entry):
    # Load the generated module without the upstream legacy versions wrapper.
    with resources.as_file(entry) as path:
        spec = util.spec_from_file_location(f"_arena_{path.stem}", path)
        if spec is None or spec.loader is None:
            raise ValueError(f"Cannot load decoder {path.name}")
        decoder = util.module_from_spec(spec)
        spec.loader.exec_module(decoder)
    return decoder


def score_replay(path, player_names=None):
    versions = resources.files("s2protocol").joinpath("versions")
    decoders = {
        int(item.name[8:-3]): item for item in versions.iterdir()
        if item.name.startswith("protocol") and item.name.endswith(".py")
        and item.name[8:-3].isdigit()
    }
    if not decoders:
        raise ValueError("No Blizzard replay decoders are installed")
    with path.open("rb") as handle:
        archive = MPQArchive(handle, listfile=False)
        header = load_decoder(decoders[max(decoders)]).decode_replay_header(
            archive.header["user_data_header"]["content"]
        )
        build = header["m_version"]["m_baseBuild"]
        if build not in decoders:
            raise ValueError(f"Missing exact decoder for SC2 build {build}")
        decoder = load_decoder(decoders[build])
        tracker = archive.read_file("replay.tracker.events")
        if not tracker:
            raise ValueError("Replay has no tracker stream")
        events = decoder.decode_replay_tracker_events(tracker)
        samples = {1: {}, 2: {}}
        for event in events:
            if not event["_event"].endswith(".SPlayerStatsEvent"):
                continue
            player = event["m_playerId"]
            loop = event["_gameloop"]
            if player not in samples or loop > header["m_elapsedGameLoops"]:
                continue
            stats = event["m_stats"]
            if all(field in stats for field in SCORE_FIELDS.values()):
                samples[player][loop] = {name: stats[field] for name, field in SCORE_FIELDS.items()}
    shared = samples[1].keys() & samples[2].keys()
    if not shared:
        raise ValueError("Replay has no complete shared sample for both players")
    last_loop = max(shared)
    rows = []
    for player, name in enumerate(player_names or ("Player 1", "Player 2"), start=1):
        components = samples[player][last_loop]
        points = components["army_minerals"] + components["army_vespene"]
        points += WORKER_POINTS * components["active_workers"]
        rows.append({"player_id": player, "name": name, **components, "points": points})
    tied = rows[0]["points"] == rows[1]["points"]
    return {
        "method": "army_minerals + army_vespene + 50 * active_workers",
        "build": build,
        "sample_game_loop": last_loop,
        "sample_seconds_on_faster": round(last_loop / 22.4, 3),
        "players": rows,
        "score_leader": None if tied else max(rows, key=lambda row: row["points"])["name"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("replay", type=Path)
    parser.add_argument("--players", nargs=2, metavar=("P1", "P2"))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.replay.resolve() == args.out.resolve():
        parser.error("Score output must differ from the replay")
    try:
        result = score_replay(args.replay, args.players)
    except (ImportError, OSError, KeyError, ValueError) as exc:
        parser.exit(1, f"Cannot score replay: {exc}\n")
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
