# 擂台版本与赛历

## 历代版本

代号是按现有实现提交回填的交付编号。Git 短号用于查询。正式比赛记录以实际参赛的完整提交为准。

| 参赛者 | 历代交付与关键修订 |
| --- | --- |
| 网页端 Pro | 第 1 代 `ea6be9c`，实战基准 `b301d0b`（CLI 修订）；第 2 代 `42d469a` |
| Codex Sol | 第 1 代 `fbc9c54`；第 2 代 `3d4470d`；第 3 代 `cb7618d`（二矿回防的步兵避开已侦测的攻城坦克射程，离线验证，待实战） |
| Antigravity（AGY） | 第 1 代 `c740e9c`；第 2 代 `7f20fad` |
| DeepSeek | 第 1 代 `cfc466d`（`ad89f42` 为文档修订）；第 2 代 `7c4513a`；第 3 代 `55bc094`；第 4 代 `2858976`；第 5 代 `32e1b50`；第 6 代 `bd79bd3`；第 7 代 `64d8375`（记录提交 `9f0967a`） |
| 混元 | 第 1 代 `f98f317`；第 2 代 `fbe071c`；第 3 代 `52daacc` |
| Mizuki | 第 1 代 `36029e4`；第 2 代 `e3e4346`；第 3 代 `91fb20e`；第 4 代 `361e7ac`；第 5 代正式锁定 `ebdc49d`；第 6 代训练 `3b17a48`；第 7 代训练 `683ef07`。`4a4ce3c`、`dfef46c` 为诊断或复盘工具修订。 |

## 已有赛历

| 阶段 | 记录 |
| --- | --- |
| 11 月 23 日环境验收 | RedCityLE 三场：实时 Bot 对 Easy、实时双 Python Bot、加速双 Python Bot。每场 180 游戏秒到时，验证进局、控制、录像和清理。 |
| 11 月 23 日三方加速验收 | Mizuki、网页端、BurnySc2 ZergRushBot 各交手一次，三局均 360 游戏秒到时。 |
| 11 月 23 日 Mizuki 重赛 | `361e7ac` 对 `b301d0b` 先跑了 6 游戏分钟到时局。取消局内上限后，`361e7ac`、`ebdc49d`、`3b17a48` 三局分别在 13:31、13:31、15:03 负于 `b301d0b`。详见下方首批录像。 |
| 11 月 24 日兼容性诊断 | DeepSeek、混元旧版对打及各自对网页端，出现客户端错误 `480098739`。`32e1b50`、`fbe071c` 各自对网页端及互赛能完赛，但混元缺少持续运营。`52daacc` 恢复补给、兵营和出兵，对 DeepSeek 时 3:37 再次异常。DeepSeek 对 Antigravity 在 8 分钟现实时间截止前稳定运行至游戏内 29:15。 |
| 11 月 24 日对照局 | 网页端第 1 代胜 Mizuki 第 5 代（11:48）；Codex Sol 第 1 代胜 AGY 第 1 代（8:27）；网页端第 2 代胜 Mizuki 第 7 代（8:38）。 |
| 11 月 24 日四家循环赛 | 网页端第 2 代 3–0，Codex Sol 第 1 代 2–1，AGY 第 1 代 1–2，DeepSeek 第 5 代 0–3。AGY 一场到时按分胜 4,850:0，其余 SC2 自然判定。详见下方循环赛总表。 |
| 11 月 24 日 GUI：DeepSeek 对 Sol | DeepSeek 第 6 代对 Sol 第 2 代。种子 2130134702 的观战尝试异常结束，无录像；同种子的加速局 Sol 胜。种子 2096761266 的重赛 Sol 胜。原始产物在本机 results/gui/20261124-deepseek-sol-*。 |
| 11 月 24 日 GUI 双场 | AGY 第 2 代 `7f20fad` 胜 Codex Sol 第 2 代 `3d4470d`，约 10:11，种子 6724；网页端第 2 代 `42d469a` 胜 AGY 第 2 代，种子 6725。均为 RedCityLE 加速模式，局内上限配置 1,800 秒。原始产物在本机 `results/gui/20261124-sol-agy-web/`。 |

- [首批三场自然结局与对比](https://github.com/RICHARDwuxiaofei/Starcarft/blob/e43adf7a4a05a99ab42840dcc0272ea6208b51f7/replays/arena-20261123/README.md)
- [六场循环赛、到时评分和迭代任务](https://github.com/RICHARDwuxiaofei/Starcarft/blob/0f9899696d1ed67e893bf9d8353e2a5e5226bb81/replays/round-robin-20261124/README.md)

## Sol 第 2 代 vs AGY 第 2 代（10:11）

- Sol 二矿 4:44 完工，AGY 二矿 3:04 完工。
- 6:49–7:03 前线交战，两边各损失 14 名陆战队员，Sol 额外损失两辆坦克。
- 8:29–8:46 二矿附近，Sol 损失 33 名陆战队员、3 架医疗运输机、1 辆坦克和二矿的星际轨道站。AGY 在这段交战中损失 1 辆架起的坦克。之后战斗进入 Sol 主基地，生产设施陆续被毁。
- 录像支持把二矿防线崩溃作为主要转折。主力为何没能及时回防，交给 Sol 在自己的实现中验证。
- 本机复盘：results/gui/20261124-sol-agy-web/01-agy-vs-sol/replay-review.json。

## Sol 第 3 代复测

Sol 第 3 代 cb7618d 对 AGY 第 2 代 7f20fad。RedCityLE、加速、种子 6724、AGY 一号位、现实时间上限 15 分钟。SC2 在 9:52 判 AGY 胜。Sol 二矿 4:42 完工，7:52 被摧毁；实际出生位置与第 2 代那局不同。运行约 2:39。录像及复盘：本机 Codex 工作区 results/codex-gen3-vs-agy-20261124-032057/match/。

## DeepSeek 第 7 代对 Sol 第 3 代

RedCityLE、加速、种子 2130134702、DeepSeek 一号位、现实时间上限 15 分钟。Sol cb7618d 在 9:37 自然获胜。DeepSeek 第 7 代代码 64d8375、记录 9f0967a，58 项离线测试通过。6:25.7，DeepSeek 人口 64/70，军队价值 950 矿、150 气，库存 60 矿；Sol 为 68/78、1600 矿、600 气。录像和复盘：本机 Codex 工作区 results/codex-ds-gen7-vs-sol-gen3-20261124-032519/match/。

## DeepSeek 第 7 代对 Mizuki 训练第 7 代

DeepSeek 9f0967a（代码 64d8375）对 Mizuki 683ef07，RedCityLE、加速、种子 98746、DeepSeek 一号位、现实上限 15 分钟。Mizuki 在 11:08 自然获胜，启动至清理约 1:40。录像及复盘：本机 Codex 工作区 results/codex-ds-vs-mizuki-20261124-032744/match/。

## Mizuki 训练第 7 代四对手复盘

训练第 7 代 683ef07 在 RedCityLE、SC2 5.0.17.98745、加速、种子 98746 下补打三场，现实上限 15 分钟，均自然判定：9:04 负 AGY 第 2 代 7f20fad，15:46 负 Sol 第 3 代 cb7618d，11:01 负网页端第 2 代 42d469af。DeepSeek 同版本的 11:08 胜局沿用，不重打。

[四场录像、复盘与第 8 代迭代任务](https://github.com/RICHARDwuxiaofei/Starcarft/blob/09afafbd396c7c27e59d7d15ba8c40706b0256f9/replays/mizuki-gen7-20261124/README.md)
