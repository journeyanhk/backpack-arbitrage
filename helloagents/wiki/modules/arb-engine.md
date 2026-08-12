# 交易执行引擎（arb-engine）

## 目的
执行"现货买入 + 永续做空"的配对开仓与平仓，保证 delta 中性并安全处理订单生命周期。

## 模块概述
- **职责:** 开/平仓编排、maker 挂单、可成交限价追单、追单失败回滚、订单状态四态管理、账本增量记账、费率硬门槛、启动对账
- **状态:** ✅稳定
- **最后更新:** 2026-08-12

## 规范

### 需求: 追单与平仓价格方向正确
**模块:** arb-engine
追单/平仓必须使用可成交限价（cross price + 滑点上限），post_only=False 只表示允许吃单，价格必须穿过盘口。

#### 场景: 现货买入追单
- 追单价 = 卖一 × (1 + 滑点上限)
- 追单超时 → 回滚已成交的永续腿（买入平仓 reduceOnly）

#### 场景: 永续卖出追单
- 追单价 = 买一 × (1 - 滑点上限)
- 追单超时 → 回滚已成交的现货腿（卖出）

#### 场景: 平仓
- 现货卖 = 买一 × (1 - 滑点上限)，intent=close（禁止 autoBorrow，允许 autoLendRedeem）
- 永续买 = 卖一 × (1 + 滑点上限)，必带 reduceOnly

### 需求: 订单状态可对账，禁止伪造成交
**模块:** arb-engine

#### 场景: 订单查询返回 OrderNotFound
- 短暂重试(1s)后仍查不到 → 状态 UNKNOWN，不假定成交、不重发同单
- 冻结该币种（_frozen），提示人工处理

#### 场景: 撤单时竞态成交
- 撤单返回后重新确认订单最终成交量（_confirm_final_fill）
- 按已确认增量记账；补单只下剩余未确认量

### 需求: 部分成交增量记账
**模块:** ledger

#### 场景: 现货全成、永续部分成交后全成
- 账本只记 delta = confirmed_filled - last_confirmed_filled
- 同一订单累计增量 = 总成交量，无漏记/重复

### 需求: 平仓只关闭策略持仓
**模块:** arb-engine

#### 场景: 账户存在人工持仓
- 平仓量 = min(账本 strategy_positions, API 总持有量)
- 启动对账：真实 vs 账本不一致 → _unknown_exposure=True，禁止开仓

### 需求: 借贷闭环
**模块:** arb-engine

#### 场景: 开仓买入现货
- autoBorrow=True（借 USDC）+ autoLend=True

#### 场景: 平仓卖出现货
- autoLendRedeem=True；autoBorrow 禁用
- 平仓后验证 USDC 债务归零

### 需求: 费率硬门槛
**模块:** arb-engine

#### 场景: 费率为负、年化低于门槛或净年化≤0
- 拒绝开仓：rate ≤ 0，或 apy < MIN_NET_APY（10%），或 (apy - EST_ROUND_TRIP_COST_APY) ≤ 0
- apy 按实际结算周期（fundingInterval）年化，缺省 3600s

### 需求: 部分成交立即追单
**模块:** arb-engine

#### 场景: 一腿部分成交、另一腿未全成
- 立即撤未全成腿并按可成交价追单（不等整腿成交）
- 追单等待 HEDGE_TIMEOUT_S（5s），未成交 → 回滚已成交腿 → EXPOSED 标记

## API接口
- 无直接 API；经 flask-api 模块调用 open_position / close_position

## 数据模型
- orders / fills / strategy_positions（详见 data.md）

## 依赖
- ccxt backpack
- ledger（SQLite）

## 已知问题与注意事项
- 追单回滚在极端行情下可能滑点超限，此时标记 EXPOSED 需人工处理
- 旧版本（v4.x）已开持仓不在账本内，升级后启动对账会禁止开仓，需人工核对后清理
- marginFraction 字段单位以 Backpack 实际响应为准，未直接用于自动强平保护

## 变更历史
- [202608121437_audit-fix](../../history/2026-08/202608121437_audit-fix/) - 按安全审计修复 P0/P1：价格方向/reduceOnly/方向识别/UNKNOWN 状态/增量记账/借贷意图/持仓归属/费率门槛/服务安全
