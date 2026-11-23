# Antigravity Terran Bot (`bots/antigravity`)

Independent StarCraft II competitive Terran bot implemented for the `burnysc2==7.3.0` arena contract (`CompetitiveBot(sc2.bot_ai.BotAI)` in `bots/antigravity/bot.py`).

## Strategy & Design

- **Core Build**: Safe Macro **1-1-1 Reaper Expand -> Marine / Marauder / Siege Tank / Medivac / Viking (+ Raven)**.
  - Opening: `14 Depot` (main ramp corner) -> `15 Refinery` -> `16 Barracks` (addon-safe ramp placement) -> `17 SCV Scout` + `19 Orbital Command` + `1 Reaper` (parallel 4-player spawn discovery on `RedCityLE`) -> `20 Command Center` (natural, or safely in main if early rush detected) -> `21 Depot` (wall completion) -> `Factory (Tech Lab)` -> `2nd Refinery` -> `Natural Bunker` -> `Starport` (1st Viking + 1st Medivac -> Reactor; 2nd Starport Tech Lab for Raven).
  - Mid/Late Scaling: Scales from 2 bases (`3 Rax [1 TL, 2 R], 1 Fact [TL], 1 SP [R], 2 Ebays`) to 3 bases (`5 Rax [2 TL, 3 R], 2 Fact [2 TL], 2 SP [1 R, 1 TL], Armory, Sensor Tower`) and 4+ bases (`up to 8 Rax, 2-3 Fact, 2 SP`, 4th+ CC morphs to Planetary Fortress when Engineering Bay is ready).
  - Upgrades: Stimpack -> Combat Shield -> Concussive Shells; Infantry `+1/+1 -> +2/+2 -> +3/+3`; Vehicle Weapons & Vehicle/Ship Armor.

## Key Engineering Highlights

1. **4-Player `RedCityLE` Native Scouting (`ag_terran/intel.py`, `ag_terran/scout.py`)**:
   - Never hardcodes `enemy_start_locations[0]`. Tracks all candidate spawns, sends SCV + 1st Reaper to distinct spawns in parallel, confirms on enemy structure sighting or by elimination once 2 of 3 spawns are visibly empty, and dispatches Vikings for late-game cleanup across unvisited expansions and map corners.
2. **Post-Harass Mining Recovery (`ag_terran/bases.py`, `ag_terran/economy.py`)**:
   - Distinguishes `LIGHT_HARASS` (small SCV defense squad with wounded pullback) from `HEAVY_HARASS` (evacuate workers to safe mineral bases, return smoothly after `harass_clear_delay=2.0s`).
   - Automatically resumes halted unfinished Terran buildings whose SCV died (`scv.smart(structure)`), repairs damaged structures/mech, rebuilds lost SCVs, and leaves zero idle workers.
3. **Outer-Base Loss Triage (`ag_terran/bases.py`, `ag_terran/tactics.py`)**:
   - When an outer base is `OVERWHELMED`, evacuates workers, cancels dying under-construction CCs, lifts threatened Command Centers / Orbitals to safety, avoids trickling army units into suicide, chooses safer alternate expansions away from the enemy main, lands lifted CCs cleanly, and only triggers `RECLAIM` once `own_army_power` outmatches the recorded threat.
4. **Proactive Pushing & Anti-Stuck Siege Tanks (`ag_terran/tactics.py`, `ag_terran/combat.py`)**:
   - Pushes proactively on Stim + 2 Tank + 2 Medivac timing (`34` army supply), army advantage (`46+` army supply), or near-max (`80+` army supply / `165+` total supply).
   - Siege Tanks siege in combat range, focus high-value targets, and automatically unsiege after `4.0s` without nearby ground targets (`unsiege_clear_delay`) using staggered leapfrog when advancing.

## References Consulted

- **Blizzard Official Patch Notes (5.0.14 – 5.0.17)**:
  - Patch 5.0.17: 12-worker start, 400-mineral Command Center giving 15 supply, 150/150 Planetary Fortress upgrade, 32s Reaper build time, Ghost & Widow Mine timings.
  - Patch 5.0.15: 125/75 Viking cost, Siege Tank abduct immunity while sieged.
  - Patch 5.0.14: Planetary Fortress 2 armor, 100/50 Sensor Tower with 22 radar range (salvageable), Extra Supplies restoring/raising Depot HP to 500, Salvage cancelling on damage.
- **Map Context (`RedCityLE`)**:
  - Liquipedia `Red City` (`160x160`, 4-player map with spawns at 2/5/8/11 o'clock, ramp-walled main, standard natural, destructible obstacles, and central Xel'Naga watchtowers).
- **`burnysc2==7.3.0` Library Source & APIs**:
  - Inspected `sc2/bot_ai.py`, `sc2/bot_ai_internal.py`, `sc2/unit.py`, `sc2/game_info.py`, `sc2/game_data.py`, `sc2/constants.py`, and `sc2/dicts/` directly in the `.venv` installation.

## Verification Scope (Offline Only — No Live SC2 Match Executed)

All checks in this round ran offline without launching StarCraft II:
- Package entrypoint import (`from bots.antigravity.bot import CompetitiveBot`) and isolated `importlib.util.spec_from_file_location` import with zero-argument instantiation `CompetitiveBot()`.
- `ruff check` and `ruff format --check` static lint/formatting checks.
- Comprehensive `pytest` offline simulation suite under `bots/antigravity/tests/` covering 4-player spawn discovery, 1-1-1 opening & macro scaling, post-harass mining recovery, outer-base loss triage & relocation, proactive push triggers, Siege Tank anti-stuck unsiege, Viking air/ground transitions, Raven Interference Matrix, and long multi-phase progression.

## Known Weaknesses & Pending Live Validation

- **Offline mocks vs real SC2 engine**: Pathing geometry, exact SC2 `find_placement` grid clearance around `RedCityLE` main ramps/destructible rocks, and real-time combat DPS/micro spacing can only be fully verified in live `RedCityLE` (`5.0.17.98745`) matches.
- **Extreme early cheese** (e.g., proxy 3-rax Reaper or 12-pool spine rush) relies on generic early-rush main-CC fallback, Bunker, and SCV pull; exact pull counts may need tuning after live replays.
- **No Medivac drop harass / Ghost late-game**: Focused first on rock-solid macro, Bio-Tank-Medivac-Viking-Raven army control, and resilience rather than multi-prong Medivac drops or Ghost EMP/Snipe micro.

## Round-Robin 2026-11-24 Iteration (`agent/antigravity-bot`)

Based on forensic review of `results/round-robin-20261124/` replays and `burnysc2==7.3.0` engine behavior:

1. **Worker Distribution & Returning-SCV Ping-Pong (`ag_terran/economy.py`, `ag_terran/macro.py`)**:
   - Fixed `HARVEST_RETURN` (`order_target == 0` / `None`) misclassification and eliminated per-tick re-gathering of oversaturated mineral workers onto the main base, alongside softening gas throttling and aligning worker cap scaling (`16` per base + `3` per refinery + `2`).
2. **Fog-of-War Expansion Blindness & Flying-CC Landing (`ag_terran/bases.py`, `ag_terran/placement.py`)**:
   - Handled `is_snapshot` mineral fields in fog of war, recognized `burnysc2`-remapped `AbilityId.LAND` orders (preventing hover oscillation and self-exclusion in `choose_next_expansion`), and kept natural Bunker placement clear of CC landing footprints.
3. **TechLab / Stimpack Priority & Endgame Closeout (`ag_terran/macro.py`, `ag_terran/intel.py`, `ag_terran/tactics.py`)**:
   - Prioritized `_manage_addons` and `_manage_upgrades` ahead of `_train_army` with strict `STIMPACK -> SHIELDWALL -> PUNISHERGRENADES` sequencing and a short Stimpack mineral reservation.
   - Tracked `enemy_main_ever_cleared` so undefended rebuilt structures do not drop a winning closeout army back into `RALLY`.
