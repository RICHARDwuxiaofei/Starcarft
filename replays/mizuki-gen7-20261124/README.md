# Mizuki 训练第 7 代｜四个对手复盘

Mizuki `683ef074e420955475e1eeb8b994049d245baf40`。对局为 RedCityLE、SC2 5.0.17.98745、加速模式，现实时间上限 15 分钟。
DeepSeek 一场沿用先前相同版本的已完成对局。其他三场为本轮新打。
若到时未自然分胜负，赛事评分为最后可用采样的军队矿值 + 气值 + 活跃工人数 × 50，同分记平。

| 对手 | 实际提交 | 结果 | 游戏时间 | 资料 |
| --- | --- | --- | --- | --- |
| DeepSeek 第 7 代 | `9f0967a` | Mizuki 胜 | 11:08 | [结果](vs-deepseek/result.json) · [录像](vs-deepseek/match.SC2Replay) · [复盘](vs-deepseek/review.json) |
| Antigravity 第 2 代 | `7f20fad` | Antigravity 第 2 代 胜 | 9:04 | [结果](vs-antigravity/result.json) · [录像](vs-antigravity/match.SC2Replay) · [复盘](vs-antigravity/review.json) |
| Codex Sol 第 3 代 | `cb7618d` | Codex Sol 第 3 代 胜 | 15:46 | [结果](vs-codex/result.json) · [录像](vs-codex/match.SC2Replay) · [复盘](vs-codex/review.json) |
| 网页端 Pro 第 2 代 | `42d469a` | 网页端 Pro 第 2 代 胜 | 11:01 | [结果](vs-webgpt/result.json) · [录像](vs-webgpt/match.SC2Replay) · [复盘](vs-webgpt/review.json) |

## 给 Mizuki 的迭代任务

从训练分支本轮参赛提交继续。对照胜局与失利，找一到两个重复出现、能用时间点和事件说明的问题。重点核对二矿完工及失守时间、前两场交战的交换与 SCV 损失。对照同一阶段的经济和军队规模，结合录像判断是否因路径、集结或指令导致无法推进。
只改自己的 Bot，沿用原创与版本规则，下一版标为第 8 代。保留正式赛锁定提交。请交付改动、离线测试和下次实战的量化复测项。实战由用户安排。
