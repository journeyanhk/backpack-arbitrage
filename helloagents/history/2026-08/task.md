# 任务清单: 审计报告安全修复（P0/P1 缺陷修复 + 轻量账本）

目录: `helloagents/plan/202608121437_audit-fix/`

---

## 1. 配置与账本基础设施
- [√] 1.1 在 `bpx_arb_ccxt.py` 中新增配置项: MIN_NET_APY（更名替代 MIN_WEEK_APY）、EST_ROUND_TRIP_COST_APY、HEDGE_TIMEOUT_S=5、MAX_SLIPPAGE_BPS、BPX_WEB_TOKEN 读取，验证 why.md#需求-费率硬门槛-场景-资金费率为负或净年化低于门槛
- [√] 1.2 在 `bpx_arb_ccxt.py` 中实现 SQLite 账本模块（orders/fills/strategy_positions/tasks 建表 + 读写函数 + client_order_id 生成），验证 why.md#需求-部分成交增量记账-场景-现货全成永续部分成交
- [√] 1.3 在 `bpx_arb_ccxt.py` 中实现启动对账（真实持仓 vs 账本，未知敞口置位 _unknown_exposure），验证 why.md#需求-平仓只关闭策略持仓-场景-账户原有XRP人工持仓

## 2. 订单生命周期与执行修复
- [√] 2.1 在 `bpx_arb_ccxt.py` 中重写 `_place_limit`（intent 参数区分开/平、借贷参数按意图、reduceOnly 支持、clientOrderId、错误脱敏），验证 why.md#需求-借贷闭环-场景-开仓买入现货
- [√] 2.2 在 `bpx_arb_ccxt.py` 中重写订单状态检查（四态 open/closed/canceled/unknown，OrderNotFound 不再伪造成交），验证 why.md#需求-订单状态可对账禁止伪造成交-场景-订单查询返回OrderNotFound
- [√] 2.3 在 `bpx_arb_ccxt.py` 中实现 `_cross_price`（带滑点上限的可成交限价）并修正追单/平仓四处价格方向，验证 why.md#需求-追单与平仓价格方向正确-场景-现货买入追单
- [√] 2.4 在 `bpx_arb_ccxt.py` 中实现撤单后重新确认最终成交量 + 增量记账函数 `_record_fill_increment`，验证 why.md#需求-订单状态可对账禁止伪造成交-场景-撤单时竞态成交

## 3. 开仓/平仓重构
- [√] 3.1 在 `bpx_arb_ccxt.py` 中重写 `execute_pair`（部分成交立即追单、HEDGE_TIMEOUT_S、回滚已成交腿、EXPOSED 标记），验证 why.md#需求-追单与平仓价格方向正确-场景-追单若在滑点上限内无法成交回滚
- [√] 3.2 在 `bpx_arb_ccxt.py` 中重写 `_get_real_position`（signed contracts × contractSize 换算、方向识别），验证 why.md#核心场景方向识别
- [√] 3.3 在 `bpx_arb_ccxt.py` 中重写 `close_pair`（平仓方向 + reduceOnly + 禁止借币），验证 why.md#需求-追单与平仓价格方向正确-场景-永续买入平仓
- [√] 3.4 在 `bpx_arb_ccxt.py` 中重写 `open_position`（leverage 参与 target_notional、保证金校验、费率硬门槛、账本登记），依赖任务3.1/3.2，验证 why.md#需求-费率硬门槛-场景-资金费率为负或净年化低于门槛
- [√] 3.5 在 `bpx_arb_ccxt.py` 中重写 `close_position`（按账本策略持仓平仓、min(账本量,可卖量)、USDC 债务归零验证），依赖任务1.2/3.3，验证 why.md#需求-平仓只关闭策略持仓-场景-账户原有XRP人工持仓

## 4. 展示与 API 安全
- [√] 4.1 在 `bpx_arb_ccxt.py` 中更新 `_build_state`（perp_qty 带符号、未知敞口标记、任务状态），验证 why.md#核心场景展示
- [√] 4.2 在 `bpx_arb_ccxt.py` 中将 POST 接口改为任务模式（/api/open、/api/close 返回 task_id）+ 新增 GET /api/task/<id>，验证 why.md#需求-服务安全
- [√] 4.3 在 `bpx_arb_ccxt.py` 中实现 POST 认证（X-Auth-Token）、127.0.0.1 监听、实盘缺 key/缺 token 拒绝启动、错误响应脱敏，验证 why.md#需求-服务安全-场景-局域网内他人访问
- [√] 4.4 在 `templates/bpx_arb.html` 中注入 token 并实现任务轮询展示，依赖任务4.2/4.3

## 5. 安全检查
- [√] 5.1 执行安全检查（按G9）: 密钥不进代码与日志、错误响应脱敏、autoBorrow 仅开仓买入、reduceOnly 全覆盖、费率硬门槛、实盘启动硬校验、监听地址与认证

## 6. 文档更新
- [√] 6.1 更新 `.gitignore`（新增 arb_ledger.db）
- [√] 6.2 更新 `README.md`（账本/费率门槛/安全说明）
- [√] 6.3 创建知识库: `helloagents/CHANGELOG.md`、`helloagents/project.md`、`helloagents/wiki/overview.md`、`helloagents/wiki/arch.md`、`helloagents/wiki/api.md`、`helloagents/wiki/data.md`、`helloagents/wiki/modules/arb-engine.md`

## 7. 测试与验证
- [√] 7.1 语法检查 `python -m py_compile bpx_arb_ccxt.py`，验证 DRY-RUN 无 key 模式可启动、/api/state 正常返回
- [√] 7.2 DRY-RUN 全链路: 开仓（费率门槛拦截 + 正常路径）、平仓、撤单、/api/task 查询，账本落库正确
- [√] 7.3 按审计"必须测试的故障场景"表核对代码路径: 部分成交立即追单/回滚、撤单竞态重确认、OrderNotFound→UNKNOWN、重启后对账、reduceOnly 防反向、卖单禁借币
