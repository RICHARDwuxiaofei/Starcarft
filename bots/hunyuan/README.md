# Hunyuan Terran Bot

原创人族竞技 Bot（混元参赛方）。目标环境：RedCityLE，SC2 `5.0.17.98745`，
依赖 `burnysc2==7.3.0`。

## 入口

```python
from bots.hunyuan.bot import CompetitiveBot

bot = CompetitiveBot()          # 无参数实例化
# sc2.main.run_game(..., sc2.player.Bot(sc2.Race.Terran, bot), ...)
```

`CompetitiveBot` 继承 `sc2.bot_ai.BotAI`，`__init__(self)` 无额外参数。
对局中只使用正常视野与合法 API，决策完全本地运行；不使用在线模型/外部服务。

## 结构

- `bot.py`：唯一与 SC2 交互的适配层。把观察转换为纯数据 `Snapshot`，执行规划器输出的 `Order`。
- `planner.py`：纯决策核心（无 sc2 依赖），包括运营、补给、扩张、侦察、防守、进攻波次和恢复状态机。
- `model.py`：纯数据模型（dataclass）。
- `notes/RESEARCH_AND_DESIGN.md`：资料查阅与设计取舍（含掉线排查记录）。
- `tests/`：37 个离线测试，覆盖入口导入、开局、补给、扩张、科技、侦察、骚扰恢复、
  首波进攻失败恢复、三矿长局、灾难性损失恢复，以及 API 请求预算与指令去重。

## API 请求预算（掉线排查后新增，不改变战术）

- `on_start` 显式把 `client.game_step` 固定为 4。
- 每步最多执行 1 次建造/扩张；普通建筑 `find_placement` 半径 4，扩张半径 5。
- 不再调用 `expand_now()`（其 `get_next_expansion()` 会对每个矿点发一次 `query_pathing`）；
  改为按几何距离选最近的未占矿点，零 pathing 请求。
- 同一单位每步最多一条非排队指令，同名指令 0.75 秒内不重复下发。
- 气井只从己方基地 14 距离内选取。
- 诊断日志默认写到系统临时目录 `hunyuan_sc2_diag.jsonl`，可用环境变量
  `HUNYUAN_DIAG_PATH` 覆盖；记录每步指令数、建造/放置请求数、掉线前的最后事件。
  写日志失败不会影响比赛。

## 离线验收（已执行）

在 `D:\CODE\Starcarft\.venv`（Python 3.12.10 / burnysc2 7.3.0）中：

```
python -m py_compile bots/hunyuan/*.py bots/hunyuan/tests/*.py    # 通过
python -m ruff check bots/hunyuan/                                # All checks passed
python -m pytest bots/hunyuan/tests -q                            # 37 passed
python -c "from bots.hunyuan.bot import CompetitiveBot; CompetitiveBot()"  # 通过
```

未启动 SC2，未做真实对局；胜绩不作声明。实战验证项见交付汇报。
