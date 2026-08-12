# Changelog

本文件记录项目所有重要变更。
格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/),
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [5.0.4] - 2026-08-12

### 修复
- ★ 追单超时回滚必须先撤追单单并重确认最终量，且只回滚未对冲部分：
  原实现直接回滚已成交腿，追单单仍挂在交易所，晚成交（如 5s 窗口刚过即成交）
  会留下裸敞口（实测: 现货追单晚成交 9.7 XRP，合约已回滚 → 裸多 ~$10）
- 回滚数量 = 已成交腿 - 已对冲部分（部分追单成交时不再整腿回滚）

## [5.0.3] - 2026-08-12

### 修复
- ★ 借贷闭环：平仓卖出现货增加 autoBorrowRepay（卖出所得自动归还 USDC 借款），债务不再残留
- ★ 债务读取改用 collateral 顶层字段 borrowLiability（原查找的逐币种字段不存在，导致
  "无法确认债务状态"；实测当前账户 borrowLiability=0，此前债务实际已由平台自动结清）
- 债务未归零时平仓结果判定为不成功（ok=false），需人工处理（审计要求）
- clientId 参数改用 ccxt 文档参数名 clientOrderId（自动转 uint32）

## [5.0.2] - 2026-08-12

### 修复
- ★ 订单状态查询改用三级对账（未成交列表 → 订单历史 → 成交记录）：ccxt.backpack 不支持
  fetchOrder()（NotSupported），原实现查单永远失败 → 订单即使成交也被当未成交反复撤单重挂，
  且撤单时"Order not found"（实际已成交）被误判为撤单失败
- 撤单 BadRequest("Order not found") 视为订单已关闭，重新确认最终成交量

## [5.0.1] - 2026-08-12

### 修复
- ★ clientId 改为 uint32 整数（Backpack 要求 integer(uint32)，字符串会 400 拒绝导致实盘无法下单）
- ★ 启动对账改为"真实持仓 ∪ 账本"并集双向比对：账本有而交易所没有的幽灵持仓（如 DRY-RUN 残留）也会被标记并禁止开仓
- 下单 400 被拒时日志明确提示"订单未接受"，不再误导为"可能已接受"

## [5.0.0] - 2026-08-12

### 新增
- 可选代理支持（BPX_PROXY）：ccxt 不读环境变量代理，需显式配置 ex.proxies
- SQLite 轻量账本（orders/fills/strategy_positions/tasks），订单生命周期与策略持仓持久化，重启可恢复
- 启动对账：真实持仓 vs 账本，发现未知敞口禁止开仓
- 费率硬门槛：费率为负、年化低于门槛或净年化（含成本缓冲）≤0 均拒绝开仓；按实际结算周期年化
- leverage 参与目标名义计算（target_notional = notional × leverage）
- 追单/回滚：任一腿有成交立即追单（带滑点上限的可成交限价），追单失败回滚已成交腿
- 服务安全：只监听 127.0.0.1；POST 接口 X-Auth-Token 认证；实盘缺 key/缺 token 拒绝启动
- 任务模式：/api/open、/api/close 返回 task_id，/api/task/<id> 查询真实执行结果
- 订单状态四态（open/closed/canceled/unknown），unknown 冻结币种等待人工处理
- 永续平仓强制 reduceOnly，防反向开仓
- 借贷按意图区分：仅开仓买入允许 autoBorrow；平仓卖现货禁止借入标的币
- 平仓后验证 USDC 债务归零

### 变更
- 追单/平仓价格方向修正：可成交限价（买一/卖一对侧 + 滑点上限），不再反挂
- 合约持仓按方向识别（signed contracts × contractSize 换算基础资产），perp_qty 带符号展示
- 平仓只关闭账本记录的策略持仓，不动人工持仓
- PAIR_TIMEOUT_S 默认 180 → 60（缩短裸露窗口）
- 错误响应脱敏，不向前端返回原始交易所错误
- MIN_WEEK_APY 更名为 MIN_NET_APY（净收益口径）

### 修复
- OrderNotFound 不再伪造成交 1 个币，改为 UNKNOWN 状态
- 撤单后重新确认最终成交量，按已确认增量记账，杜绝部分成交漏记/重复记账
- 撤单与成交竞态不再重复下单（先重确认再补单）
- 订单行支持按 order_id 或 client_order_id 双键查找（修复 DRY-RUN 下 dry 订单查不到导致超时的问题）
- 费率门槛口径修正：年化 ≥ 门槛且净年化 > 0 才放行（原先误将门槛作用于净年化，11% 年化会被误拒）

## [4.1.0] - 2026-08-0X

### 修复
- 维持保证金率改用 marginFraction（账户级），不再误用 mmf（单品种参数）

## [4.0.0] - 2026-08-05

### 变更
- 底层从 bpx-py SDK 迁移到 ccxt 4.x
- 精度交给 ccxt（amount_to_precision / price_to_precision）
- 错误处理 ccxt 类型化异常
- 抵押品数据用 implicit API privateGetApiV1CapitalCollateral()
