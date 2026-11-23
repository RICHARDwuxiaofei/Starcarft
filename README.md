# Starcarft · StarCraft II Bot 擂台

Starcarft 是一个用 Python 和 burnysc2 开发、运行和对比原创 StarCraft II Bot 的项目。各参赛者在独立分支开发，由统一对局程序在本机加载 Bot，保存录像与结果，再据此迭代。

- 默认人族，入口为 `bots/<agent>/bot.py` 中可无参数实例化的 `CompetitiveBot`。
- 当前本机验收环境为 STUDIO、Python 3.12、burnysc2 7.3.0、SC2 5.0.17.98745 和 RedCityLE。
- 已完成本机实时和加速双 Bot 对局。云端用于同步代码、共享复盘和离线验证。实战由用户安排。
- 对局使用正常视野与本地决策。原创、版本编号、工作区和观战规则见 [AGENTS.md](AGENTS.md)，接口见 [比赛约定](docs/ARENA_CONTRACT.md)。

## 资料索引

- [历代版本与比赛记录](docs/ARENA_HISTORY.md)
- [环境检查](tools/check_environment.py)
- [首批完整录像与复盘](https://github.com/RICHARDwuxiaofei/Starcarft/blob/e43adf7a4a05a99ab42840dcc0272ea6208b51f7/replays/arena-20261123/README.md)
- [四家循环赛录像与复盘](https://github.com/RICHARDwuxiaofei/Starcarft/blob/0f9899696d1ed67e893bf9d8353e2a5e5226bb81/replays/round-robin-20261124/README.md)

## 环境检查

在 STUDIO 的仓库根目录运行已有 Python 环境：

```powershell
$env:SC2PATH = 'C:\baoxue\StarCraft II'
.\.venv\Scripts\python.exe tools\check_environment.py
```

游戏安装、地图和本地对局产物存放在各自原目录。仓库中的 `results/` 默认忽略，经过选择的复盘资料单独发布到 GitHub 资料分支。
