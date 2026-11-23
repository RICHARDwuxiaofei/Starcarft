"""Rebuild arena Elo ratings from a ledger of naturally decided matches."""

import argparse
import json
from collections import Counter
from pathlib import Path


def calculate(ledger):
    baseline = ledger["baseline"]
    factor = ledger["k"]
    ratings = {}
    wins = Counter()
    losses = Counter()
    latest = {}
    seen = set()
    for match in sorted(ledger["matches"], key=lambda row: (row["finished_at"], row["id"])):
        match_id = match["id"]
        winner = match["winner"]
        loser = match["loser"]
        if match_id in seen or winner == loser:
            raise ValueError(f"Invalid or repeated match: {match_id}")
        seen.add(match_id)
        first = ratings.get(winner, baseline)
        second = ratings.get(loser, baseline)
        expected = 1 / (1 + 10 ** ((second - first) / 400))
        movement = factor * (1 - expected)
        ratings[winner] = first + movement
        ratings[loser] = second - movement
        wins[winner] += 1
        losses[loser] += 1
        latest[winner] = match["winner_commit"]
        latest[loser] = match["loser_commit"]
    return {
        "scope": ledger["scope"],
        "baseline": baseline,
        "k": factor,
        "rated_matches": len(seen),
        "players": [
            {"agent": name, "elo": round(elo, 2), "wins": wins[name], "losses": losses[name],
             "last_rated_commit": latest[name]}
            for name, elo in sorted(ratings.items(), key=lambda row: (-row[1], row[0]))
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledger", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    ledger = json.loads(args.ledger.read_text(encoding="utf-8-sig"))
    result = calculate(ledger)
    output = json.dumps(result, indent=2) + "\n"
    if args.out:
        if args.out.resolve() == args.ledger.resolve():
            parser.error("Output must differ from input")
        args.out.write_text(output, encoding="utf-8")
    print(output, end="")


if __name__ == "__main__":
    main()
