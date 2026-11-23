# 给 Codex 的任务

你的唯一可编辑 Bot 目录是 `bots/codex/`，不得修改 `bots/antigravity/` 或 `bots/other/`。建议使用分支 `agent/codex-bot` 和独立 worktree。

你是一个独立开发《星际争霸2》本地 AI Bot 的 Agent。仓库：D:\\CODE\\Starcarft（实际 Windows 路径 D:\CODE\Starcarft）。使用已有 Python venv 中的 burnysc2==7.3.0，库的导入名称是 sc2，绝对不要 pip install 旧包 sc2。请先阅读 README.md 和 docs/ARENA_CONTRACT.md，确认 SC2 游戏版本 5.0.17.98310 和本机 SC2PATH。仅在你被分配的目录编辑，你不得读写、复制、利用另一个 Agent 的 Bot 代码；共享文件、maps、results、replays 和 Git 根目录不允许修改，必要的公共接口变更先报告。

开发任务：独立设计一个可运行的 Python BotAI 子类 CompetitiveBot，支持自动分配采矿、维持农民生产、补人口、建造科技建筑、训练军队、侦察、攻击与基础微操；选择 Terran（人族），初始对战资源和条件与对手完全相同。请自己设计战术，不参考对方实现。保留清晰中文注释和合理的日志，不在比赛期间调用在线大模型或外部服务。不要写依赖暴雪普通 Battle.net 房间的联机逻辑。

必须交付：bot.py（可导入 CompetitiveBot）、你的目录下的 README.md（战术、依赖、运行限制）、tests/（可在不启动 SC2 的条件下验证基本导入和逻辑的测试）。通过 python -m compileall 和本地静态检查；单机游戏实测需要我明确批准之后再启动。不要直接修改 main 或提交到主分支；在自己独立 Git 分支或 worktree 内开发并提供 PR。不要修改另一个 Agent 的文件。

公平测试约束：统一地图、种族、客户端版本、realtime 设置、地图出生点交换、对战次数与录像保存方式；Bot 不得获得对手视野之外的信息。你只开发你的战术 AI，不控制另一位 Bot 的策略，不擅自改动测评框架。
