# API 手册

## 概述
本地 Flask 服务（端口 5055，仅监听 127.0.0.1）。交易类 POST 接口需要认证。

## 认证方式
- 整个面板（页面 + 全部 API）需要登录：`POST /api/login` 提交 BPX_WEB_USER / BPX_WEB_PASSWORD
- 登录成功写入 HttpOnly session cookie，浏览器自动携带；未登录访问 API 返回 401，访问页面重定向到 /login
- 登录接口防爆破：同 IP 60 秒内失败 5 次锁定
- 实盘（BPX_LIVE=1）未配置登录凭据直接拒绝启动

---

## 接口列表

### 面板

#### GET /
**描述:** 交易面板页面（注入 WEB_TOKEN）

---

### 状态

#### GET /api/state
**描述:** 当前状态（持仓/订单/余额/日志/风控标记）

**响应:**
```json
{
  "dry_run": true,
  "has_key": false,
  "positions": {"MON": {"spot_qty": 0, "perp_qty": -100, "perp_side": "short"}},
  "active_orders": {},
  "balances": [],
  "maintenance_margin_ratio": null,
  "total_assets_value": null,
  "strategy_ledger": [{"symbol": "MON", "spot_qty": 0, "perp_qty": -100}],
  "unknown_exposure": false,
  "frozen": [],
  "exposed": [],
  "logs": []
}
```

#### GET /api/symbols
**描述:** 可选币种列表（抵押品 ∩ 永续 + 最新费率 APY）

**响应:**
```json
[{"symbol": "MON", "max_leverage": 3.3, "haircut": 0.7, "latest_rate": 0.001, "latest_apy": 87.6}]
```

#### GET /api/task/<task_id>
**描述:** 查询后台任务（开仓/平仓）真实执行结果

**响应:**
```json
{"task_id": "open-1786517167466", "status": "done", "result": {"ok": true, "pairs_done": 2, "pairs_total": 2}}
```
**status:** running / done / error

---

### 交易（需认证）

#### POST /api/open
**描述:** 开仓（异步任务，结果由 /api/task 查询）

**请求参数:**
| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| symbol | string | 是 | 币种代码（如 MON） |
| notional | number | 是 | 本金 USDC（目标名义 = notional × leverage） |
| leverage | number | 否 | 杠杆倍数（默认 1） |
| order_size | number | 否 | 单笔拆单 USDC（默认 100） |
| timeout | number | 否 | 单对超时秒数（默认 60） |

**响应:**
```json
{"ok": true, "task_id": "open-xxx", "msg": "已提交后台执行"}
```

**错误码:**
| 错误码 | 说明 |
|--------|------|
| 400 | 参数无效 |
| 401 | 未授权 |
| 409 | 币种操作进行中 / 已冻结 / 未知敞口禁止开仓 |

#### POST /api/close
**描述:** 平仓账本记录的策略持仓（异步任务）

**请求参数:**
| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| symbol | string | 是 | 币种代码 |
| order_size | number | 否 | 单笔拆单 USDC |

**响应:** `{"ok": true, "task_id": "close-xxx"}`

#### POST /api/cancel
**描述:** 撤销该币种全部未成交订单

**请求参数:** `{"symbol": "MON"}`
