# Mizuki · SC2 Terran Bot

为 Starcarft Bot Arena 独立编写的 Terran（人族）AI。入口是
`from bots.mizuki.bot import CompetitiveBot`，种族传 `Race.Terran`。
不修改比赛框架、其他参赛者目录或游戏设置；对战期间不联网调用模型，不关闭战争迷雾，不读其他玩家的内部数据。

## 战术

- **开局：** 持续造 SCV、预判人口缺口、兵营与气矿、死神探路。根据基地威胁决定先补兵还是存矿开二矿。建造位会检查附属建筑预留空间；不在一个无效扩张点无限重试。
- **中后期：** 机枪兵/掠夺者搭配坦克与运输机，补 Stim、盾牌、震撼弹和步兵攻防。短时保留已看见的敌方空军情报，增加维京；重甲提高掠夺者比例。侦测风险升高时留雷达能量并补防空塔。
- **交战：** 保护受威胁基地，基于兵力、接触和集结程度选择守家、推进或后撤。坦克前后梯队变形，步兵按射程和冷却开火、拉扯，并避开侦测到的灵能风暴与腐蚀胆汁；有足够交战收益才打兴奋剂。运输机留在步兵后方，维京优先截击空军。没看到目标时按地图候选点搜索，而不是锁定未侦察的建筑。
- **版本：** 仓库记录目标客户端为 `5.0.17.98310`，库为 `burnysc2==7.3.0`。[暴雪 5.0.17 正式补丁](https://news.blizzard.com/en-us/article/24309308/starcraft-ii-5-0-17-patch-notes)恢复了 12 农民开局与 400 矿指挥中心；实际建造成本由游戏数据决定，不写死旧版的 300 矿。阵容优先级、交战阈值与时机是初始启发式，需要实战录像校准，不是已证实的最佳配比。

## 运行

从仓库根目录，PowerShell；使用自己的已安装游戏和已配置 Python 环境：

```powershell
py -3.12 -m pip install -r requirements.txt
py -3.12 -m compileall -q bots/mizuki
py -3.12 -m pytest -q bots/mizuki/tests
py -3.12 -m ruff check bots/mizuki
$env:SC2PATH = 'C:\baoxue\StarCraft II'  # 按本机安装位置修改
py -3.12 -m bots.mizuki.smoke --map RedCityLE --race Terran --difficulty Medium --seed 17 --replay replays/mizuki-tvt-17.SC2Replay
```

`RedCityLE` 必须是本机 Maps 目录里已安装的地图文件名（不带扩展名）。默认加速运行，`--realtime` 可以观战；超过 `--seconds` 上限的平局不算胜场。启动器只用于单机打内置 AI，不加入 Battle.net。正式对比双方 Bot 时，在中立裁判脚本中按仓库规则交替出生点并查看录像，不用一次内置 AI 测试代替分边赛。

## 验证边界

无客户端测试覆盖入口、视野过滤、工人和人口决策、同帧人口/升级预算、扩张失败切换、交战状态与启动参数。云端没有与比赛一致的 Windows 游戏客户端，因此这里没有实战赛果，也不承诺胜率。第一轮建议分别跑 TvT、TvZ、TvP，检查开局不卡人口、二矿时点、第一批坦克/运输机时点、首战是否整个队伍跟上，然后每组用相同种子与交换出生点的基准复测。

## 回放：只生成 AI 复盘 JSON

安装一次额外依赖，使用同一套 Python 解释器；只分析回放时不用启动或安装游戏。仓库根目录 PowerShell：

```powershell
.\.venv\Scripts\python.exe -m pip install -r bots/mizuki/requirements-replay.txt
.\.venv\Scripts\python.exe -m bots.mizuki.replay_review replays/mizuki-tvt-17.SC2Replay --out results/mizuki-review.json
```

或者让本机单局试跑直接在结束后生成 JSON：

```powershell
.\.venv\Scripts\python.exe -m bots.mizuki.smoke --map RedCityLE --race Terran --difficulty Medium --seed 17 --replay replays/mizuki-tvt-17.SC2Replay --review-json results/mizuki-review.json
```

把 `results/mizuki-review.json` 发给分析的 AI 即可。多个录像路径可以一次传入；报告依次列出地图、版本、胜负、开局、首个关键单位、各次扩张、经济/人口采样、损失和候选交战。不含聊天、账号 ID 或本机绝对路径；`--anonymous` 可再移除显示名称和原文件名。需要定位具体交战时加 `--include-events` 输出完整的规范化 tracker 时间线（比默认摘要大）。不上传录像、不调用在线服务，也不渲染网页。

候选交战根据归因给对手的单位损失聚类，不代表所有交战；人口是采样，不能据此精确计算卡人口时长；回放不能说明 Bot 为什么下了某个指令。解析使用回放自己的 base build，不会用近似协议猜结果。单个录像失败会写入 `errors`，同批成功结果仍会保留；任何失败会返回非零退出码。
