# Starcarft — SC2 Python Bot Arena

A shared repository for **independently authored StarCraft II bots** using [`burnysc2`](https://github.com/BurnySc2/python-sc2). The initial setup does not contain bots or a functioning match harness.

## Machines

| Role | Machine | SC2 path | Python |
| --- | --- | --- | --- |
| Primary development | STUDIO | `C:\baoxue\StarCraft II` | 3.12 |
| Secondary match/test | WXFLAPTOP | `D:\baoxue\StarCraft II` | 3.13 |

Both clients were inspected at build `5.0.17.98310`. Match interoperability has **not** been executed.

## Install / activate

Windows PowerShell, at repository root:

```powershell
py -3.12 -m venv .venv      # laptop: py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:SC2PATH = 'C:\baoxue\StarCraft II'  # laptop: D:\baoxue\StarCraft II
.\.venv\Scripts\python.exe tools\check_environment.py
```

`import sc2` is correct; the PyPI distribution name is `burnysc2`, not the retired `sc2` package.

## Project layout

- `bots/codex/`: agent Codex only
- `bots/antigravity/`: agent Antigravity/Gemini only
- `bots/other/`: optional other agents
- `docs/ARENA_CONTRACT.md`: shared bot interface and fair-match rules
- `prompts/`: identical task specification with workspace-specific boundaries
- `maps/`: local maps (ignored by Git)
- `replays/`, `results/`: local-generated outputs (ignored by Git)
- `tools/check_environment.py`: read-only environment checker

## Maps and networking

`RedCityLE.SC2Map` has been downloaded to both SC2 installation `Maps` directories, with identical SHA-256 `C32E64E6AEB40EBAAC3F662A15558E4615369BFA19386AD96236800012B40593`. Source: https://github.com/ttinies/sc2gameMapRepo/blob/master/sc2maptool/maps/RedCityLE.SC2Map. Map file loading has not yet been tested in-game. Local map binaries are not in Git. Both machines currently share `192.168.5.0/24`, with bidirectional ICMP confirmed. No tunneling is needed between those two machines. Remote-friend games should first try a private overlay (Tailscale / ZeroTier) and a separately implemented, tested SC2 API host/join flow. A Battle.net lobby is not the same as SC2 API multiplayer.

## Git workflow

Develop on local STUDIO. Push to GitHub to synchronize. Three independent Git worktrees have been created: `D:\CODE\Starcarft-agent-worktrees\codex` (`agent/codex-bot`), `D:\CODE\Starcarft-agent-worktrees\antigravity` (`agent/antigravity-bot`), `D:\CODE\Starcarft-agent-worktrees\other` (`agent/other-bot`). Each agent may edit only its own `bots/<agent>/` folder within its assigned worktree, and should use `D:\CODE\Starcarft\.venv\Scripts\python.exe` as the existing Python environment. The integration owner reviews and merges changes. Do not have multiple agents edit the same checkout at the same time. The existing test file `1` is preserved.
