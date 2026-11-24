# Sol 自我对战客户端异常 · 2026-11-24

这是供独立分析的诊断资料。对局在 RedCityLE，SC2 5.0.17.98745、burnysc2 7.3.0、加速模式。

- [完整事故报告](SC2_RUNTIME_INCIDENTS.md)，来自本机共享记录 `9406b45`。
- [第 3 代对第 4 代，5:20 崩溃](01-g3-vs-g4/native-crash-partial.SC2Replay) · [末尾事件](01-g3-vs-g4/crash-last-10s.json)
- [第 5 代镜像，4:59 崩溃](04-gen5-full-selfplay/native-crash-partial.SC2Replay) · [末尾事件](04-gen5-full-selfplay/crash-last-10s.json)
- [第 5 代镜像，8:00 短测到时](03-gen5-stability/match.SC2Replay)
- [第 2 代对第 3 代，11:55 自然结束](05-old-g2-vs-g3/match.SC2Replay)

四个目录也保留了两侧采样、游戏结果和控制台日志，其中本机路径已代换。两份崩溃录像是客户端保存的片段。请求种子相同，但以录像证实的实际出生点为准。

分析时先追原生客户端的 `480098739`，结合两份报告都有的 `Mismatch in texture reduction DB`。比较相同场景、出生组合和客户端配置，区分图形/资源状态、对局脚本请求与 Bot 行为。后续 WebSocket 关闭、SDK 断言和录像是否保存按实际先后顺序判断。请给最小对照矩阵和下一轮应记录的证据。

[混元崩溃对照：三份部分录像和原生错误摘录](hunyuan/README.md)
