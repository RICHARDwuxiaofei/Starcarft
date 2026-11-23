# 混元人族 Bot：资料查阅与设计取舍

本文件记录本 Bot 的原创设计依据、查阅过的公开资料、以及关键取舍。
所有实现均为独立设计，未读取任何其他人族参赛者的会话、分支、代码、测试、
配置、日志、录像或复盘，也未复制/移植/包装任何现成 Bot 或示例战术框架。

## 1. 查阅的资料（均为公开规则/依赖/接口）

### 仓库共享规则
- `main` 最新版（`origin/main` @ 2757433）的 `AGENTS.md`
- `docs/ARENA_CONTRACT.md`（`main` 最新版）
- `README.md`、`requirements.txt`（`origin/main`）

要点：
- Python + `burnysc2==7.3.0`（导入名 `sc2`），入口 `CompetitiveBot(sc2.bot_ai.BotAI)`，可无参数实例化。
- 只用正常视野和合法游戏 API，不做全图透视/debug 刷资源/外部决策服务。
- 目标地图 RedCityLE，客户端平衡版本 `5.0.17.98745`，对局由统一脚本在两个出生点交换后多局进行。
- 固定随机种子时应尽量可重复。

### 公开的游戏机制/地图资料
- 天梯 1v1 地图为双人镜像地图；标准基地 8 片矿 + 2 气井；主矿位于高地，天然分矿靠近主矿；
  存在 Xel'Naga 瞭望塔、可破坏岩石、多进攻路径等常见要素（通用地图设计资料）。
  - 来源：通用 SC2 地图/地图池公开介绍文章；pysc2 `docs/maps.md` 对 LE/TE 地图的说明。
- 结论：Bot 不硬编码 RedCityLE 的具体坐标，而是通过 `game_info` / `expansion_locations_list`
  与 `enemy_start_locations` 在运行时推导矿点、分矿和进攻目标，保证换边可复现。

### burnysc2 7.3.0 官方库源码（接口事实，仅取接口与行为）
所在：`D:\CODE\Starcarft\.venv\Lib\site-packages\sc2\`
- `bot_ai.py`：`BotAI` 基类。
  - `on_start` / `on_step(iteration)` / `on_end` 生命周期。
  - `time`（game_loop/22.4）、`start_location`、`enemy_start_locations`。
  - `expansion_locations_list`（快速候选分矿点）。
  - `expand_now(building=None, max_distance=10, location=None)`、`get_next_expansion()`。
  - `can_afford(item_id, check_supply_cost=True)`。
  - `already_pending(unit_type_or_upgrade)`、`worker_en_route_to_build`、`structure_type_build_progress`。
  - `find_placement(...)`、`build(building, near, max_distance=20, ...)`。
  - `train(unit_type, amount=1, closest_to=None, train_only_idle_buildings=True)`：
    会考虑科技前置、 reactors/techlab、supply 与资源，并对 reactor 自动补第二条队列。
  - `research(upgrade_type)`：自动找 idle 的对应建筑，检查前置。
  - `distribute_workers(resource_ratio=2)`：矿气分配与closest-expansion 分配。
- `bot_ai_internal.py`：`self.do(...)` 把 `UnitCommand` 加入 `self.actions`，帧末统一提交；
  `self.units/workers/structures/townhalls/...` 每帧由框架更新，均为正常视野内友军；
  `self.enemy_units/enemy_structures` 为当前可见敌军；`known_enemy_*` 未在视野内也包含近期记忆。
- `unit.py`：`unit.attack(target)`、`unit.move(pos)`、`unit.gather(target)`、
  `unit.build(...)`、`unit.train(...)`、`unit.research(...)`、`unit(ability, target)`；
  属性 `is_ready/is_idle/is_structure/build_progress/health_percentage/add_on_tag/orders/
  weapon_cooldown/is_flying/position/distance_to`。
- `ids/unit_typeid.py` / `ids/ability_id.py` / `ids/upgrade_id.py`：单位/能力/升级 ID 枚举。
- `units.py`：`Units` 容器的过滤/排序/中心点等辅助方法。

没有把库自带的任何示例 Bot 代码复制进实现；只按其公开接口调用。

## 2. 目标与打法设计（原创）

### 定位
一套 **稳健经济 + 阶梯式推进** 的人族生物/机械化混编打法（内部代号“铁砧”）。核心不是
复刻某套职业开局，而是用一套通用、可恢复的状态机，让 Bot 在无人干预下：
先把经济与科技立稳，再以成建制部队分波推进；被打退后能回防并继续运营。

### 资源与运营原则
1. **永续 SCV 生产**：每个已完成的指挥中心（Command Center）持续补 SCV，直到达到
   目标数量（分矿数 × 22，封顶 80）；被骚扰减员后会自动补齐。
2. **补给优先**：`supply_left` 低且 `already_pending(SUPPLYDEPOT)` 不足时优先补补给站，
   并在-critical 时优先于部队生产，避免卡人口。
3. **矿气平衡**：使用框架 `distribute_workers` 做基础分配；当气体储备超出部队需求时
   降低采气比例，把多余 SCV 放回矿区。气体优先供给科技/高级单位。
4. **扩张阶梯**：主矿饱和后自动开天然分矿；分矿建成并累积到一定矿量后再开第三矿；
   之后按“已占矿点 < 目标扩张数 且 资源溢出”继续扩张。扩张与防守绑定：新矿点周边
   出现敌人时由防守部队处理。

### 生产与科技树
- 开局：CC → 补给站 → 兵营（Barracks）→ 精炼厂（Refinery）→ 第二个补给站 → 工厂（Factory）
  → 星港（Starport）；指挥中心适时升级为 Orbital Command（增加 scan 视野与 MULE 经济）。
- 部队构成（按阶段递进，不是一次性全解锁）：
  - 早期：Marine（机枪兵）为主，少量 Firebat/Reaper 不做；以防守与首次推进为核心。
  - 中期：加入 Marauder（劫掠者，需科技实验室）与 Siege Tank（攻城坦克，需工厂挂科技实验室），
    搭配 Medivac（医疗运输机，需星港）。
  - 后期：补 Engineering Bay / Armory 升级 + 增兵工厂（双兵营/双星港）提高量产。
- 升级：一旦有科技实验室就研究 Stimpack、Combat Shield、Concussive Shells；
  攻防升级按资源情况推进（步兵武器/护甲、车辆武器）。
- 补给站默认不升降；不依赖复杂 addon 交换，降低实现风险。

### 侦察
- 开局派一名 SCV 进行“到点即走”的侦察：先去敌方可能的天然分矿/主矿边缘查看，
  再返回采矿；途中发现敌方建筑/部队时记录威胁等级。
- Orbital Command 能量充足时对敌方主矿/集结点使用 Scan，用于确认对手走向与分矿。
- 不读取对方进程/内存/未公开信息；所有情报来自可见单位与合法 API。

### 防守
- 维护一个“防守集合点”：主矿与最近分矿之间的位置。
- 当敌方地面单位进入我方矿点/主基地半径内，或警报 `BuildingUnderAttack` 触发：
  - 就近召回部分部队回防，优先攻击靠近矿区/建筑的敌人。
  - 必要时拉回少量 SCV 协同（只在近战/近矿威胁时，且数量受控，避免经济崩盘）。
- 被骚扰减员后由“永续 SCV 生产”和补给优先逻辑自动恢复经济。

### 进攻
- 采用“部队价值阈值 + 集结”机制：当可战部队规模/价值超过阈值且集结完毕，
  发起一次推进（attack-move 到目标分矿或敌方主矿方向）。
- 首轮进攻失败（部队大幅减员/被清退）不会触发 all-in：状态机回到运营，
  继续补兵、升级、扩张，攒够后发动下一波。
- 战斗中简单微操：受伤单位后撤、坦克保持攻城模式输出、医疗机跟随、 stim 在接敌时启用，
  但所有微操都基于本地可见信息。

### 确定性
- 使用固定种子 `random.Random(SEED)` 驱动任何随机决策；不在对局中调用在线服务。
- 时间/距离阈值使用 game_loop 与游戏内单位距离，避免依赖真实时钟。

## 3. 关键取舍与风险
- **取舍：不追求最快职业开局，追求“不会卡死”的完整闭环。** 对 bot 对抗而言，
  能持续生产、补补给、恢复骚扰损失，比极限 timing 更有价值。
- **取舍：不使用自定义 placement 网格算法**，直接用框架 `find_placement/build`，
  减少地形边界 bug；代价是建筑布局不最优。
- **取舍：部队微操保持轻量**（集合、focus fire、后撤、坦克攻城），避免复杂寻路导致卡住。
- **风险：地图具体矿点坐标只有运行时可知**，换边/不同出生点必须靠 `expansion_locations_list`
  和敌人出生点推导；已设计为运行时计算。
- **风险：SC2 5.0.17.98745 与本地环境 5.0.17.98310 的微小差异** 不影响 API 使用，
  但需实战验证（本次不启动 SC2）。

## 4. 本次验证范围（离线）
- 入口导入：`from bots.hunyuan.bot import CompetitiveBot` 且可无参实例化。
- 静态检查：ruff / py_compile。
- 关键场景离线测试：用桩对象（fake state/units）驱动 `on_step`，验证
  开局下单、补给、SCV 生产、扩张触发、被骚扰后恢复、首轮进攻失败后继续运营等逻辑分支。
- **未做**：任何真实 SC2 对局；胜绩不做声明。

## 5. 客户端掉线排查（授权范围内的只读对照）

### 授权范围与证据来源
- 只读取自己 `webgpt-vs-hunyuan` 对局目录的 `console.log` / `result.json`；
  没有查看 `webgpt-vs-deepseek` 或其他参赛者的目录。
- 对照只读取 `b301d0b9b8c230b2f5ca2ba82b962b167f704bfd:bots/webgpt/` 中与
  **客户端初始化 / 推进步长 / API 请求提交与等待**有关的文件（`run.py`、`runtime.py`、
  `bot.py` 的 on_start、`config.py` 的请求预算、macro 中查询次数上限），不取其战术实现。
- 事实：本局 hunyuan 客户端（端口 62300）在 156.52 游戏秒（约 2:37）断连；
  日志在 1:53 记录到“敌方单位出现在我方基地附近”；结束后 `main.run_game`
  收到 `TimeoutError` + `ClientConnectionResetError`，未形成正式胜负。

### 可疑点与排除方法
1. **每帧无界的 API 查询突发（首要）**。原实现 `_do_expand()` 调用库 `expand_now()`：
   `get_next_expansion()` 对每个扩张点执行一次 `await client.query_pathing()`，随后
   `find_placement(placement_step=1, max_distance=10)` 最多再发约 10 批放置查询；
   同时 `plan()` 一帧可输出多个建造订单，每个 `build()` 内部最多 6 批放置查询。
   `b301d0b` 的兼容实现把 pathing 查询限制为 6 个候选点、把 `find_placement` 调用
   限制为每步 4 次（`max_placement_queries`），并且从不使用 `expand_now()`。
   **修复**：扩张改为纯几何选点（零 pathing RPC）+ 半径 5 的 `find_placement`；
   每步最多 1 次建造/扩张；普通建筑查询半径从 12 降到 4。
2. **同帧重复/冲突指令**。战斗单位每帧全体重发 `attack`/`move`，防守时 SCV 同时收到
   `attack` 与 `gather`。兼容实现通过 `unit_command_uses_self_do=True` 把所有命令交给
   `CommandBus` 统一仲裁（一单位一帧一主命令 + 0.65 秒重复命令抑制）。
   **修复**：不改动库级 self_do 模式（避免影响 `build()`/`distribute_workers()`），
   在适配层加等价的本地仲裁：一单位一步一指令 + 0.75 秒同名指令冷却。
3. **跨图气井指令**：`_free_geyser()` 原来会选到任何可见气井，可能让 SCV 跨图造精炼厂。
   **修复**：只选己方基地 14 距离内的气井。
4. **步长差异（基本排除）**：兼容实现在 `on_start` 显式设置 `client.game_step = 4`；
   本场日志 877 步 / 156.52 游戏秒也正好是 game_step=4，说明步长并非直接差异。
   仍显式固定为 4 并记录旧值，便于确认。

### 诊断埋点
`HUNYUAN_DIAG_PATH`（默认系统临时目录 `hunyuan_sc2_diag.jsonl`）记录 start/frame/
expand_attempt/build_error/expand_error/end 事件，含每步指令数、建造次数、放置请求数、
pathing 请求数、失败次数与最后错误。下一场重点看掉线前最后一个 `frame` 行的
`commands` / `placement_queries` 以及是否有 `build_error`、`expand_error`。

### 仍未验证
真实对局仍未由本方启动；上述修复是降低已知风险，不能宣称已经定位为唯一根因。
需要在下一场用诊断日志确认：掉线前是否仍出现查询/指令峰值，或是否在某个具体
`build_error`/`expand_error` 之后立刻发生。
