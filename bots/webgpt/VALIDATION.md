# webGPT Bastion 1.1.0 验证记录

记录日期：2026-11-23。分支 `webGPT-replay-iteration`，父版本 `b301d0b`。
本轮在独立云端 worktree 中开发与检查，所有变更限于 `bots/webgpt/`。

## 本轮实际执行结果

| 项目 | 结果 |
| --- | --- |
| 云端 Python | 3.12.14，Linux |
| 游戏库 | `burnysc2==7.3.0` |
| 测试工具 | `pytest==9.2.0`、`ruff==0.17.6` |
| `python -m pytest bots/webgpt/tests -q` | **178 passed in 1.28s**，完整最终运行；时间不是 SC2 性能指标 |
| `python -m ruff check bots/webgpt` | **All checks passed** |
| `python -m ruff format --check bots/webgpt` | **26 files already formatted** |
| `python -m compileall -q bots/webgpt` | 退出码 0 |
| 导入和构造 `CompetitiveBot(seed=42)` | 通过；仍是 `BotAI` 子类，版本 1.1.0，seed 保留 |
| `python -m bots.webgpt.run --help` | 退出码 0，中文帮助正常；没有启动游戏 |
| `git diff --check` | 通过 |
| 新版真实 SC2 对局 / 用户电脑操作 | **未执行**，由用户统一安排 |
| 新版 Windows 原生验证 | **本轮未执行**；跨平台逻辑仍需用户环境确认 |
| 新分支 GitHub Actions | 本轮没有运行；继承的 push 过滤器只匹配原 `webGPT` 分支，未改工作流 |

首次完整静态检查发现新增测试文件的一处导入排序问题，修正后上述最终静态检查通过。
独立审查还发现“全部基地暂时消失、后来恢复基地”时重整点未重新绑定的边界，已修复并加入回归用例。
之后进行了上述完整 178 项验证。实际测试没有执行真实 SC2 对局或创建 SC2 客户端进程。

## 本轮新增的 46 项验证

相对于基线的 132 项测试，新增 46 项。既有撤退测试也更新为新约定：11 秒仍保持撤退，12 秒且归队后可恢复。

| 文件 | 新增项数 | 验证的具体风险 |
| --- | ---: | --- |
| `tests/test_engagement.py` | 27 | 真实 ScoreDetails 字段；累计军队损失不被补兵抵消；击杀工人/建筑的收益；自身经济损失不计军队战损；窗口裁剪、同帧重复、观察中断、非法数据、计数器/时钟倒退及新攻势清零 |
| `tests/test_strategy.py` | 13 | 新矿不拖动锁点；锚定基地失去/起飞以及所有基地失去后恢复；归队按战力而非单位数；24 秒不能绕过；空军队解除重整而不出击；配置边界 |
| `tests/test_replay_iteration_integration.py` | 6 | 正常分数观察贯通完整调度，产生撤退 MOVE/UNSIEGE 和遥测；有利交换继续进攻；归队和最短时间约束；旧亏损不重复触发；家防优先后恢复义务；固定锁点 |

真实对象联动链为：

`SC2 protobuf → GameState / ScoreDetails → BotAI._prepare_step → CompetitiveBot.on_step → UnitCommand`

沿用基线的合成地图、单位数据与 `OfflineClient` RPC 替身。
新增测试把累计损失、击杀分数及部队位置放进正常协议观察，没有替换策略或战斗控制函数。
一个核心场景保持军队人口 **53**，更换部分单位 tag，注入 **700** 军事损失，验证仍会撤退。
对照场景同样损失 700，但取得 700 经济/科技击杀收益，验证不会因新增交换门槛误撤。
这些都是针对风险构造的观察，**不是从三份 JSON 完整恢复出的实战时间线**。

## 基线结果与新版结果分开

基线 `b301d0b` 原有 132 项离线测试，本轮完整运行仍覆盖它们。
原版曾通过 Linux/Python3.12 和 Windows/Python3.13 的 [GitHub Actions](https://github.com/RICHARDwuxiaofei/Starcarft/actions/runs/45150128889)。
这是基线 CI，不能当成新分支已经通过 Windows CI。

用户共享的 [三场实战资料](https://github.com/RICHARDwuxiaofei/Starcarft/blob/e43adf7a4a05a99ab42840dcc0272ea6208b51f7/replays/arena-20261123/README.md)
确认原版在 RedCityLE、SC2 `5.0.17.98745` 的三场比赛中全部获胜。
本轮依据其配套 JSON 分析时间、坐标与损失；未自行播放客户端录像或安排新实战。
新版本 1.1.0 的胜率、损失改善与结束时间尚无实战数据。

## 尚待实战确认

- 30 秒 / 600 资源 / 1.8 倍门槛是否适合当前地图与对手，是否及时止损且不过度中断有收益的进攻。
- 全局击杀收益是否掩盖另一个局部战场的不利交换。
- 12 秒 / 70% 武装战力 / 16 距离的重整要求是否过慢；实际寻路、地形与堵路是否导致长期等待。
- 地图和引擎的真实指令执行、当前补丁的计分更新节奏及性能；离线合成夹具无法验证这些。

详细证据、实现取舍和下一轮指标见 [REPLAY_ITERATION.md](REPLAY_ITERATION.md)。
本轮未开启 PR、未合并、未修改 `main` 或其他参赛分支，未读取其他参赛者实现。
