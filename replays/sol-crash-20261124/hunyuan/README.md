# 混元跨 Bot 崩溃对照

三份均为 SC2 5.0.17.98745、RedCityLE、加速模式下客户端自己保存的部分录像。原生报告为 P2，附只包含错误分类及相关字样的摘录。

| 对局 | 冻结版本 | 结果 |
| --- | --- | --- |
| [混元初版对 DeepSeek](01-vs-deepseek-initial/partial.SC2Replay) | 混元 f98f317，DeepSeek 2858976 | 约 2:23 掉线 |
| [混元初版对网页端](02-vs-webgpt-isolated/partial.SC2Replay) | 混元 f98f317，网页端 b301d0b | 约 2:37 掉线 |
| [混元恢复运营后对 DeepSeek](03-vs-deepseek-ops-restored/partial.SC2Replay) | 混元 52daacc，DeepSeek 32e1b50 | 约 3:37 掉线 |

混元 fbe071c 的中间修复版曾分别对网页端和 DeepSeek 正常结束，但录像显示开局三名 SCV 后基本没有建造或持续生产，故只能视为低运营负载的通过样本。52daacc 恢复运营，约 0:18 开工补给站、0:51 开工兵营、1:56 出第一名陆战队员、2:46 开工二矿、3:24 开工重工厂，随后再次异常。相邻事件仅供定位，不单独作为触发证据。

结合 [Sol 事故资料](../README.md)，核对相同错误码之外还有哪些共同点，尤其区分 SC2 原生报错和后续 SDK 断开。混元三个样本同为 P2，分析时保留席位和实际出生点这两个变量。
