"""Read-only StarCraft II environment diagnostic. Does not launch SC2."""
from __future__ import annotations

import importlib.metadata as metadata
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
SC2PATH = os.environ.get("SC2PATH")
candidates = [Path(SC2PATH)] if SC2PATH else []
candidates.extend([Path(r"C:\baoxue\StarCraft II"), Path(r"D:\baoxue\StarCraft II")])
game = next((p for p in candidates if p.is_dir() and (p / ".build.info").exists()), None)
print(f"Python: {sys.version.split()[0]} / {sys.executable}")
for package in ("burnysc2", "pytest", "ruff"):
    try:
        print(f"{package}: {metadata.version(package)}")
    except metadata.PackageNotFoundError:
        print(f"{package}: NOT INSTALLED")
try:
    from sc2.bot_ai import BotAI
    from sc2.player import Bot
    print("SC2 Python imports: PASS")
except Exception as exc:
    print(f"SC2 Python imports: FAIL ({type(exc).__name__}: {exc})")
print(f"SC2PATH variable: {SC2PATH or '(unset)'}")
print(f"Game directory: {game or 'NOT FOUND'}")
if game:
    builds = sorted((p for p in (game / "Versions").glob("Base*") if (p / "SC2_x64.exe").is_file()), reverse=True)
    for folder in builds:
        print(f"SC2_x64.exe: {folder / 'SC2_x64.exe'}")
    for line in (game / ".build.info").read_text(encoding="utf-8-sig").splitlines()[1:2]:
        print("Build info: " + (line.split("|")[-2] if "|" in line else line[:100]))
game_maps = game / "Maps" if game else None
for path in (PROJECT / "maps", game_maps):
    if path:
        maps = list(path.glob("*.SC2Map")) if path.exists() else []
        print(f"Map directory {path}: {len(maps)} direct .SC2Map file(s)")
        for m in maps[:10]:
            print("  " + m.name)
print("SC2 process: NOT STARTED (diagnostic only)")
