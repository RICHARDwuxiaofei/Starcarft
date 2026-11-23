# Bot-vs-bot development contract

- Both agents write separate implementations under `bots/codex/` and `bots/antigravity/`.
- Export a subclass of `sc2.bot_ai.BotAI` in `bot.py`; favor a class named `CompetitiveBot` for each.
- Bot entrypoints must not modify shared code, opponents' files, the game installation, or OS settings.
- Do not use online model APIs during a match unless explicitly permitted. Code-based bots should be deterministic given a supplied random seed where practical.
- Compare both bots under the same race, map, game version, resource rules, and game duration. Rotate spawn sides. Run repeated series, not one match.
- A later neutral harness should import both Python classes, call `sc2.main.run_game` with two `sc2.player.Bot` instances, record winner, seed, duration, and replay. Use `realtime=True` for a human-friendly live view and `False` for accelerated evaluation.
- For an isolated live spectator, use observer support only after validation; bot client windows do not imply full map vision.
- Cross-machine games need more than `SC2_x64.exe -listen -port`; coordinate API host/join, game ports, firewall rules and NAT/overlay carefully. Do not expose API websocket ports publicly.
- No game or agent code is currently implemented or tested. Environment preparation alone is not match validation.
