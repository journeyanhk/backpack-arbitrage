# 架构设计

## 总体架构
```mermaid
flowchart TD
    A[Flask API<br/>127.0.0.1 + X-Auth-Token] --> B[open_position / close_position]
    B --> C[execute_pair / close_pair]
    C --> D[订单生命周期<br/>open/closed/canceled/unknown]
    D --> E[SQLite 账本<br/>orders / fills / strategy_positions]
    C --> F[ccxt Backpack<br/>现货 + 永续]
    G[启动对账<br/>真实持仓 vs 账本] --> B
    E --> H[_build_state 展示]
```

## 技术栈
- **后端:** Python 3.10+ / Flask 3.x / ccxt 4.x
- **前端:** 原生 HTML/JS（无框架，Flask render_template 注入 token）
- **数据:** SQLite（标准库，arb_ledger.db）

## 核心流程

### 开仓
```mermaid
sequenceDiagram
    User->>Flask: POST /api/open {symbol, notional, leverage}
    Flask->>Engine: open_position (异步任务)
    Engine->>Gate: 费率硬门槛 + 保证金 + 杠杆校验
    Engine->>Engine: target_notional = notional × leverage → 拆对
    Engine->>Engine: 每对: 现货买 maker + 永续卖 maker
    Engine->>Ledger: 订单登记 + 已确认增量记账
    Engine->>Engine: 任一腿成交 → 立即追单（可成交限价+滑点上限）
    Engine->>Engine: 追单超时 → 回滚已成交腿
    Flask-->>User: /api/task/<id> 查询真实结果
```

### 平仓
```mermaid
sequenceDiagram
    User->>Flask: POST /api/close {symbol}
    Flask->>Engine: close_position (异步任务)
    Engine->>Ledger: 读取策略持仓（现货 + 永续空头）
    Engine->>Engine: 现货卖（禁借币）+ 永续买（reduceOnly）
    Engine->>Ledger: 按成交增量减记
    Engine->>Engine: 验证 USDC 债务归零
```

## 重大架构决策

| adr_id | title | date | status | affected_modules | details |
|--------|-------|------|--------|------------------|---------|
| ADR-001 | 采用 SQLite 账本而非内存状态 | 2026-08-12 | ✅已采纳 | ledger, arb-engine | [详情](../../history/2026-08/202608121437_audit-fix/how.md#adr-001-采用-sqlite-账本而非内存状态) |
| ADR-002 | 平仓归属采用"账本策略持仓" | 2026-08-12 | ✅已采纳 | arb-engine | [详情](../../history/2026-08/202608121437_audit-fix/how.md#adr-002-平仓归属采用账本策略持仓而非子账户或-client-order-id-全量归属) |
| ADR-003 | 追单失败优先回滚已成交腿 | 2026-08-12 | ✅已采纳 | arb-engine | [详情](../../history/2026-08/202608121437_audit-fix/how.md#adr-003-追单失败优先回滚已成交腿) |
