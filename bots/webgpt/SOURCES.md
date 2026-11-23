# 资料与原创边界

## 原创声明

webGPT 的策略、运营决策、战术控制、微操、比赛报告与离线测试在本分支独立实现。此次开发没有把现成 Bot、开源示例 Bot 或其他参赛 Agent 的代码作为实现材料，也没有读取 mizuki 等对手的策略、测试或配置。使用 burnysc2 作为通用 API 客户端，不等于自行实现 SC2 协议，也不宣称依赖库为原创。

此前环境准备阶段曾另行下载并试过参考项目；那些文件没有被本次开发读取、复制或移植。本次查阅范围限定为下面的官方机制、协议、库 API 及安装包内接口实现。

## 查阅资料

查询基准日期：2026-11-23。网页文档可能跟随 develop 更新，代码兼容检查以隔离环境安装的 `burnysc2==7.3.0` 为准。

| 来源 | 用途 |
| --- | --- |
| [Blizzard SC2 新闻索引](https://news.blizzard.com/en-us/feed/starcraft-2) | 核对当前官方正式更新与 PTR 的区别 |
| [5.0.17 正式更新日志](https://news.blizzard.com/en-us/article/24309308/starcraft-ii-5-0-17-patch-notes) | 核对12工人开局、400矿指挥中心、15人口及5.0.16平衡回退 |
| [5.0.15 正式更新日志](https://news.blizzard.com/en-us/article/24225313/starcraft-ii-5-0-15-patch-notes) | 维京价格与坦克等沿用机制；预算仍读取运行时游戏数据 |
| [5.0.15 Hotfix](https://news.blizzard.com/en-us/article/24240447/starcraft-ii-5-0-15-hotfix-patch-notes) | 确认最初正式日志后来还受热修调整，避免将历史值当作当前值 |
| [BurnySc2 BotAI API](https://burnysc2.github.io/python-sc2/bot_ai/index.html) | 建造、科技、pending、技能查询、地形与可通行性接口 |
| [BurnySc2 Unit API](https://burnysc2.github.io/python-sc2/unit/index.html) | 可见性、隐形、附件、攻击范围、武器冷却和命令接口 |
| [BurnySc2 GameData API](https://burnysc2.github.io/python-sc2/game_data/index.html) | 动态成本、变形增量成本与游戏帧单位 |
| [BurnySc2 技能枚举](https://github.com/BurnySc2/python-sc2/blob/develop/sc2/ids/ability_id.py) | 核对技能名称；未查阅仓库中的示例 Bot |
| [Blizzard Raw API 协议](https://github.com/Blizzard/s2client-proto/blob/master/s2clientprotocol/raw.proto) | 区分真实可见单位、历史快照、隐形状态与合法动作 |
| [Blizzard C++ 技能枚举](https://github.com/Blizzard/s2client-api/blob/master/include/sc2api/sc2_typeenums.h) | 核对技能目标类型；Python名称以 burnysc2 枚举为准 |
| [GitHub actions/checkout](https://github.com/actions/checkout) | 云端自动检查的仓库检出接口与当前v7用法 |
| [GitHub actions/setup-python](https://github.com/actions/setup-python) | 云端Python环境与当前v7用法；不涉及Bot策略 |

此外检查了 `burnysc2==7.3.0` 安装包内 `main.py`、`player.py`、`paths.py`、`maps.py` 等基础库接口，以确认运行函数签名、返回结果、路径检查和地图查找行为；未读取它的示例 Bot。

## 已据资料作出的实现决定

- 游戏预算尽量读取客户端提供的成本和当前资源，不沿用旧补丁的开局经济假设。
- 直接攻击与历史侦察位置分开；快照不当作本帧可攻击的目标。可见位置也不自动代表隐形目标已被侦测。
- `get_available_abilities` 的结果是逐单位的嵌套列表；技能可用不代表目标位置、距离和落点都已通过校验。
- 所有对局固定 `disable_fog=False`，不提供调试资源、全图视野或在线大模型决策。
- `run_game` 对内置电脑返回单个结果，对 Bot/人类组合返回两个客户端结果；不会虚构内置电脑的独立返回值。
- 库的时间上限会返回 `Tie`，但天然平局也可能返回 `Tie`。报告保留原始结果，不仅凭 `Tie` 就断言超时，更不会将超时判成胜利。
- `--swap` 交换镜像、人类或指定 Bot 的客户端槽位。内置电脑模式不支持这个选项，因为库要求首位是可加入游戏的 Bot/Human。地图是否按槽位确定物理出生点，仍需实战或回放核对。

## 验证边界

云端开发期间只执行语法、导入、静态检查和离线模拟测试，未调用用户台式机或笔记本，未启动真实 SC2。测试中的游戏状态、对手、API 返回值和回放字节均为构造数据；它们验证代码契约与异常处理，不能证明真实地图加载、游戏运行、战术强度或胜率。

`--check` 只做文件及依赖检查，不会启动游戏，因此也不能验证显卡初始化、实际对局或离线授权。正式胜负只能依据之后真实比赛的结果报告与回放。
