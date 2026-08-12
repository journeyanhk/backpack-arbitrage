# 数据模型

## 概述
SQLite 账本（arb_ledger.db）持久化订单生命周期、已确认成交增量、策略持仓与后台任务。
账本写入使用每次操作新建连接（check_same_thread 天然安全）。

---

## 数据表/集合

### orders（订单）

**描述:** 每笔下单的登记与生命周期（请求量、状态、已确认累计成交量）

| 字段名 | 类型 | 约束 | 说明 |
|--------|------|------|------|
| id | INTEGER | 主键自增 | |
| client_order_id | TEXT | 唯一 | 策略生成的订单 ID（arb-{symbol}-{intent}-{ts}） |
| order_id | TEXT | | 交易所订单 ID |
| symbol | TEXT | 非空 | 币种代码 |
| market | TEXT | 非空 | spot / perp |
| side | TEXT | 非空 | buy / sell |
| intent | TEXT | 非空 | open / close |
| requested_amount | REAL | 非空 | 请求数量 |
| price | REAL | | 下单价格 |
| status | TEXT | | new/open/closed/canceled/unknown |
| last_confirmed_filled | REAL | 默认0 | 上次已确认累计成交量 |
| reduce_only | INTEGER | 默认0 | 是否 reduceOnly |
| created_at / updated_at | TEXT | | ISO 时间 |

### fills（成交增量）

**描述:** 按"已确认增量"记录，杜绝漏记/重复

| 字段名 | 类型 | 约束 | 说明 |
|--------|------|------|------|
| id | INTEGER | 主键自增 | |
| order_id / client_order_id | TEXT | | 关联订单 |
| symbol | TEXT | | 币种代码 |
| market | TEXT | | spot / perp |
| side | TEXT | | buy / sell |
| qty | REAL | 非空 | 本次新增成交量（增量） |
| price | REAL | | 成交均价 |
| ts | TEXT | | 记账时间 |

### strategy_positions（策略持仓）

**描述:** 策略自有持仓（唯一真实来源），perp_qty 带符号（空头为负，基础资产单位）

| 字段名 | 类型 | 约束 | 说明 |
|--------|------|------|------|
| symbol | TEXT | 主键 | 币种代码 |
| spot_qty | REAL | 默认0 | 现货持仓量 |
| perp_qty | REAL | 默认0 | 永续持仓量（空头为负） |
| updated_at | TEXT | | 最后更新时间 |

**更新规则:** 仅由 fills 增量驱动：spot buy + / sell -；perp sell（开空）- / buy（平多）+。

### tasks（后台任务）

| 字段名 | 类型 | 约束 | 说明 |
|--------|------|------|------|
| id | TEXT | 主键 | open-{ts} / close-{ts} |
| action / symbol | TEXT | | 动作与币种 |
| status | TEXT | | running/done/error |
| result | TEXT | | JSON 结果 |
| created_at / updated_at | TEXT | | ISO 时间 |
