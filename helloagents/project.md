# 项目技术约定

---

## 技术栈
- **核心:** Python 3.10+ / ccxt 4.x / Flask 3.x
- **数据:** SQLite（标准库 sqlite3，账本文件 arb_ledger.db）
- **前端:** 原生 HTML/JS 单页（templates/bpx_arb.html，无框架）

---

## 开发约定
- **代码规范:** PEP 8；文件顶部保留版本说明 docstring
- **命名约定:** 内部函数下划线前缀（`_xxx`）；常量大写
- **注释语言:** 中文（代码标识符/API 名除外）
- **版本号:** 记录在 bpx_arb_ccxt.py 文件头 docstring 与 helloagents/CHANGELOG.md

---

## 错误与日志
- **策略:** 交易错误脱敏，前端只收到通用信息，细节进服务端日志
- **日志:** logging（bpx_arb.log 文件 + 控制台），级别 INFO；操作日志同时进 operation_log 供前端展示
- **订单状态:** 四态 open/closed/canceled/unknown；unknown 必须冻结币种人工处理，绝不假定成交

---

## 风控与安全
- **默认 DRY-RUN**（BPX_LIVE != 1）；实盘启动硬校验缺 key/缺登录凭据拒绝启动
- **监听地址:** 仅 127.0.0.1；整个面板需登录（BPX_WEB_USER/BPX_WEB_PASSWORD）
- **借贷:** autoBorrow 仅开仓买入现货；平仓卖现货禁止借入标的币
- **永续平仓:** 强制 reduceOnly
- **费率:** 开仓硬门槛（净年化 ≥ MIN_NET_APY），费率为负拒绝开仓
- **持仓归属:** 平仓只按账本 strategy_positions 记录量执行，不动人工持仓

---

## 测试与流程
- **测试:** python3 test_bpx_arb_v5.py（mock 交易所，不连真实网络）
- **语法检查:** python -m py_compile bpx_arb_ccxt.py
- **提交:** 遵循仓库现有风格（docs:/fix:/cleanup: 前缀）
