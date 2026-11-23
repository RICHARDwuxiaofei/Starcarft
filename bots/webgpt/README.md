# webGPT Bastion — 原创人族对战 Bot

这是 `webGPT` 分支的完整参赛实现。统一入口为
`bots.webgpt.bot:CompetitiveBot`，继承 `sc2.bot_ai.BotAI`，使用 **burnysc2 7.3.0**。

战术主轴是**持续扩张的生化部队 + 坦克阵地**，以医疗运输机维持续航，根据侦察结果补维京和渡鸦。
没有接入在线大模型；开发完成后，比赛中的决策由这些 Python 模块在本地执行。

**目前完成了云端离线验证，没有运行真实 SC2 比赛。** 测试记录见 [VALIDATION.md](VALIDATION.md)，
本项目不会把通过测试解释成战术强度、胜率或已经击败任何 Agent。

## 原创及隔离

开发遵循仓库根目录的 [AGENTS.md](../../AGENTS.md)：

> 不准抄别人，可以查资料和机制，以及最新的官方更新日志什么的。

本轮没有读取 mizuki 或其他参赛者的实现，也没有采用第三方 Bot、示例 Bot 或现成战术框架。
查询范围是游戏机制、Blizzard 官方补丁、SC2 协议和 burnysc2 库接口；详见 [SOURCES.md](SOURCES.md)。
所有 Bot 文件都在 `bots/webgpt/`，分支是 `webGPT`。开发与检查在云端完成，没有调用用户电脑。

## 实现了什么

| 系统 | 行为与作用 |
| --- | --- |
| 经济 | 持续训练 SCV，按矿/气饱和程度分配工人，避险、维修和恢复中断施工，空闲矿骡重新找矿 |
| 供给 | 按生产槽位预测人口需求；优先坡口位置，堵住时使用普通位置；地面敌军靠近时升起补给站 |
| 扩张 | 根据工人和战况扩张，检查距离、公开敌情和可放置位置；为指挥中心预留预算，最后基地丢失后优先重建 |
| 生产 | 兵营/重工/星港及附件；为附件留空间，堵住时起飞搬迁；反应堆使用两个槽位并正确排队 |
| 科技 | 轨道指挥部、刺激剂、战斗盾牌、震撼弹、生化攻防和机械攻防；实际可用技能与价格由客户端查询 |
| 情报 | SCV 侦察、按最后看见的时间衰减记忆、隐形科技预警；雾中快照不能当作当前可打击目标 |
| 战略 | 集结、回防、进攻、撤退、搜索五个状态，进退有迟滞；敌情未知时也会主动寻找目标 |
| 生化微操 | 目标优先级、射程与冷却驱动的风筝、刺激剂条件判断、范围危险躲避 |
| 坦克 | 根据接敌距离架起，接触消失后收起，处理最小射程及撤退，避免不断架收 |
| 空军支援 | 医疗机治疗分摊和防空避险；维京应对空军/巨像并在合适条件下落地收尾；渡鸦探测护航 |
| 防御支援 | 隐形威胁附近扫描、保留扫描能量、矿骡经济、条件性防空塔，以及受压时地堡驻防 |
| 工程控制 | 单帧预算防重复花钱、同单位指令仲裁、能力批量查询、附近敌军空间索引、结果与战术日志 |

这不是逐帧规划最优解的战斗模拟器。战力估算、兵种比例、出击人口和微操阈值都是可解释的启发式，
真正打过后应根据回放调整。没有实现空投编队、幽灵技能链、战列巡航舰路线或跨机器 LAN 协调器。

## 安装与检查

从本分支仓库根目录运行。需要 Python 3.12 或 3.13、暴雪官方 SC2 客户端以及本地地图。
项目锁定的发行包是 `burnysc2`；代码里的 `import sc2` 是正确导入名。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m bots.webgpt.run --check --sc2-path "C:\baoxue\StarCraft II"
```

`--check`只检查包版本、可执行文件位置和地图读取，不启动游戏。
笔记本可使用 Python 3.13，并把游戏根目录换成 `D:\baoxue\StarCraft II`。
如已在主仓库配置了 `.venv`，可直接使用该解释器的绝对路径，无需重新安装。

默认地图 `RedCityLE`，对应游戏 `Maps` 目录内的 `RedCityLE.SC2Map`。
运行器不下载、不上传地图。历史准备记录中的两台机器已有该地图，但本轮没有访问电脑重新确认。

### 保持其他 Agent 的工作目录独立

回到电脑后，可在原仓库里**另建 worktree**，首次使用且目标路径不存在时运行：

```powershell
Set-Location D:\CODE\Starcarft
git fetch origin webGPT
git worktree add -b play/webGPT D:\CODE\Starcarft-agent-worktrees\webGPT origin/webGPT
Set-Location D:\CODE\Starcarft-agent-worktrees\webGPT
& D:\CODE\Starcarft\.venv\Scripts\python.exe -m bots.webgpt.run --check --sc2-path "C:\baoxue\StarCraft II"
```

不用在 mizuki 正在编辑的目录里切分支，也不要为重复执行上述命令加入 `--force`。
仓库根目录 `AGENTS.md` 的全局规则位于主分支；既有分支需要主动读取最新版规则。

## 开始比赛

### 对内置电脑

```powershell
.\.venv\Scripts\python.exe -m bots.webgpt.run --opponent computer --difficulty medium --opponent-race zerg --realtime
```

难度可选 `very-easy`、`easy`、`medium`、`medium-hard`、`hard`、`harder`、`very-hard`。
未提供作弊难度。去掉 `--realtime` 使用 API 的步进模式，实际运行速度取决于电脑与决策开销。
所有模式固定开启战争迷雾，没有 `--disable-fog` 选项。

### 同一台电脑，两个 Bot 实例对打

```powershell
.\.venv\Scripts\python.exe -m bots.webgpt.run --opponent mirror --realtime --seed 42
```

会启动两份 SC2 客户端，各自由独立 Bot 实例控制。电脑要能承受两份客户端；
两个窗口呈现各自的正常视角，不是额外的全图观察者。赛后可以打开保存的回放观看。

### 自己用游戏画面操作对抗 Bot

```powershell
.\.venv\Scripts\python.exe -m bots.webgpt.run --opponent human --opponent-race terran
```

这是**同机人类对战**，会自动开启实时模式。远程朋友联机仍需要另行实现与验证 API 的
CreateGame/JoinGame、游戏端口及网络协调，不能靠这个参数或单独的 `-listen/-port` 自动加入房间。

### 由中立裁判加载另一个 Agent

等用户准备比赛、把两个参赛实现放进中立集成目录后，可以显式指定公开入口：

```powershell
python -m bots.webgpt.run --opponent-bot bots.other.bot:CompetitiveBot --opponent-race terran --seed 42
python -m bots.webgpt.run --opponent-bot bots.other.bot:CompetitiveBot --opponent-race terran --seed 42 --swap
```

这里 `bots.other.bot` 仅是接口占位，当前仓库没有该 Bot，必须换成用户实际准备的模块。
`--check`不会导入外部类；真正请求该比赛时才导入。开发过程中不需要读取对手实现。
`--swap`交换加入槽位，不能保证每张地图都确定交换物理出生点。比较结果时核对回放中的出生位置，
统一地图文件哈希、游戏/平衡版本、种族与时限，使用多个种子和多局数据。

两个独立 worktree 不会自动让双方模块出现在同一个 Python 进程里；裁判应在独立目录整合选定提交，
不要为运行对战去覆盖任一 Agent 的工作区。内置电脑模式受库入口限制，不能使用 `--swap`。

## 参数与结果

| 参数 | 默认值 / 作用 |
| --- | --- |
| `--map` | `RedCityLE`，地图名不带后缀 |
| `--sc2-path` | 可选；指定含 `Versions`、`Maps` 的根目录；先于库的路径解析设置 |
| `--game-time-limit` | `1800` 游戏秒；`0`表示不限游戏时间，与现实耗时不同 |
| `--seed` | `1`，32位非负整数；写入报告并传给游戏，不能保证所有客户端行为完全确定 |
| `--output-dir` | `results/webgpt`，保存本次 JSON、JSONL 和回放 |
| `--realtime` | 按实时速度运行，适合观看 |

一次运行对应一个独立编号：

- `编号.json`：环境、地图哈希、参与者槽位、配置、API 结果、错误、回放存在性与大小。
- `编号_webgpt.jsonl`：每10游戏秒的经济/军队/决策快照，以及状态转换、错误和终局事件。
- `编号.SC2Replay`：只有客户端实际保存成功才会存在；不会生成假回放或把文件名当作成功证据。

进程退出码：`0`为有效结果且回放非空；`1`为脚本/游戏/结果异常；`2`为配置或环境问题；
`3`为取得结果但未保存有效回放；`130`为用户中断。平局或达到游戏时限不记作胜利。
如果进程被外部强杀，报告可能停在 `STARTING/RUNNING`，这不是终局结果。
命令行重定向输出统一使用UTF-8，其他程序读取其stdout/stderr时也应明确按UTF-8解码。

启动器不会自动修显卡、终止其他 Python 进程或修改防火墙。此前桌面客户端出现过 D3D9 初始化错误；
代码测试通过不能消除这个独立的运行环境问题，真实启动失败时应检查 SC2 的 `GameLogs`。

## 修改战术的入口

| 文件 | 适合修改的内容 |
| --- | --- |
| `config.py` | 决策频率、工人数上限、进退阈值与情报记忆时长 |
| `intelligence.py` | 侦察证据、记忆衰减、威胁分类与搜索路径 |
| `strategy.py` | 出击条件、基地/生产规模、应对敌军的兵种比例 |
| `macro.py` | 建造优先级、预留资源、附件、升级与出兵 |
| `economy.py` | SCV 采矿、修理、侦察和逃生 |
| `combat.py` | 单位级微操、集火、坦克、支援机、范围危险 |
| `runtime.py` | 每帧预算和指令仲裁；修改时务必跑对应回归测试 |
| `bot.py` | 模块调度、实例入口和日志 |
| `run.py` | 比赛启动、环境检查与结果报告 |

例如用户之后自己做更激进的版本，可以在构造实例时传入配置：

```python
from bots.webgpt.bot import CompetitiveBot
from bots.webgpt.config import BotConfig

bot = CompetitiveBot(config=BotConfig(attack_supply=34, max_workers=66))
```

这只构造对象，不会启动游戏；比赛由裁判或 `run.py` 调用 `run_game`。

## 开发验证

在仓库根目录执行：

```powershell
python -m pip install -r bots/webgpt/requirements-dev.txt
python -m pytest bots/webgpt/tests -q
python -m ruff check bots/webgpt
python -m ruff format --check bots/webgpt
python -m compileall -q bots/webgpt
```

测试不要求 SC2 安装，也不启动游戏。完整流程测试使用真实库对象和合成观察，只替换客户端查询。
本分支的 GitHub Actions 也只运行这些检查；后续实战要单独记录版本、场次、回放和胜负。
