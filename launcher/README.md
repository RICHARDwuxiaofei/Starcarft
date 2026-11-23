# Starcarft Arena — 本地 GUI 启动器

本目录是 **中立比赛基础设施**，不包含参赛 Bot 的策略。界面用浏览器打开，
服务仅监听本机 `127.0.0.1:8765`，不需要 Node、Electron、Flask 或在线大模型。
比赛运行时只执行选定的 Python `CompetitiveBot` 代码。

## 一键启动

在台式机 STUDIO 双击仓库根目录：

```text
D:\CODE\Starcarft\Start_Arena_GUI.bat
```

程序使用 `D:\CODE\Starcarft\.venv\Scripts\python.exe` 和 `burnysc2==7.3.0`。
如果隔离环境缺失，会在当前项目内创建并按 `requirements.txt` 安装依赖。
随后默认打开 <http://127.0.0.1:8765>。如未自动打开，可手动输入此地址。

注意：关闭浏览器页面并不等于停止 GUI 服务。关闭启动脚本的控制台可关闭本地服务。
实际游戏请优先在 GUI 点击停止；正常对战结束后可查看 Replay。

## 模型、Agent 和脚本三者的区别

- “模型 / 编写者来源”：GPT/webGPT、mizuki、Gemini、Codex 等标签，标记是谁写的。
- “Python 脚本”：选取对应工作树 `bots/<agent>/bot.py` 文件，动态加载导出的 `CompetitiveBot`。
- “备注”：在对局配置内记录模型名（例如 GPT-6 vs Gemini）。这只是标签。
- **不调用在线大模型 API**，不配置密钥，不因对战持续消耗 Token。
- 两个 Bot 可以选择同一文件实现镜像战，实际使用两个独立对象/客户端。
- 对手还可选 SC2 内置电脑或同机真人。**跨地区真人/好友联机未实现**，不能通过选择真人自动连接好友。
- 游戏默认启用战争迷雾；不开启作弊选项。

## 发现 Bot / 独立工作树

启动器只枚举当前工作区 `D:\CODE\Starcarft\bots`，以及
`D:\CODE\Starcarft-agent-worktrees\<工作树>\bots\<作者>\*bot.py` 的路径，
不会自动从 GitHub 拉取或修改其他 Agent 代码。

当前为比赛准备的隔离快照：

```text
D:\CODE\Starcarft-agent-worktrees\webGPT-arena  ← origin/webGPT 的独立快照
D:\CODE\Starcarft-agent-worktrees\mizuki-arena  ← origin/mizuki 的独立快照
```

这两个 worktree 是 detached HEAD，不会占用参赛者分支，也不会在比赛期间自动同步。
要比较更新后的提交，需先**在安全确认没有本地改动的前提下**由人工或 Agent 更新该快照。
其他 Agent 继续在各自的开发 worktree 编辑，不要让启动器负责修改源码。

支持加载多个不同工作树中都叫 `bot.py` 的文件；内部使用隔离模块名，
让 `from .config import ...` 等相对导入能够运行。参赛入口要求
`class CompetitiveBot(BotAI)`，无需在线 LLM。

## 地图及游戏客户端

启动器自动寻找 SC2 安装根目录与最新的 `Versions\Base*\SC2_x64.exe`，
不会把早期 `Base98310` 版本路径写死。当前已观测到台式机存在
`C:\baoxue\StarCraft II\Versions\Base98745\SC2_x64.exe`。
地图下拉列表读取 `C:\baoxue\StarCraft II\Maps\*.SC2Map`。
不自动下载地图，也不会修改官方游戏安装。

若游戏客户端版本更新，重新打开 GUI 即可重新检测。
**静态检查会导入参赛 Python 包以验证入口，可能执行其模块顶层代码；但不启动 SC2。**
仅使用你信任的 Agent 提交。

## 结果与日志

每一局在仓库 `results/gui/<时间戳-短ID>/` 写入：

- `config.json`：双方 Bot 文件、模型标签、地图、难度、时间限制和游戏位置
- `console.log`：该次 Python SC2 API 运行日志
- `result.json`：真实终局结果、错误、回放是否存在
- `game.SC2Replay`：只有客户端真实保存成功才会出现
- 可选 `left_telemetry.jsonl` / `right_telemetry.jsonl`

停止按钮会尝试终止**本次由 GUI 创建**的进程树；不会随意结束其他 SC2 进程。
停止时可能没有 Replay。重启 GUI 后可浏览最近比赛的摘要。

目前没有自动 BO11、多人远程主机/加入、第三方全图实时观战、API 计费模型切换等功能。
这些属于未来中立裁判/联机项目，不应伪装为已支持。

## 排错

```powershell
cd D:\CODE\Starcarft
.\.venv\Scripts\python.exe launcher\server.py --self-test
.\.venv\Scripts\python.exe -m compileall -q launcher
```

在 GUI 先执行“静态检查”以定位缺少类、导入失败、地图与客户端路径问题。
正常的 Battle.net 启动方式和 SC2 API 启动方式有差别；如果前者能打开而后者报
DirectX/连接问题，先查看 `Documents\StarCraft II\GameLogs`，不要急于重装驱动。

关于公平性和不得抄袭，请阅读仓库根目录 `AGENTS.md`。
