# -*- coding: utf-8 -*-
"""
Backpack 资金费率套利交易脚本 v5.0 — ccxt 重写版 (2026-08-12)
Flask 后端 + 前端操作面板

端口 5055 | 实盘需 BPX_LIVE=1 环境变量 | 默认 DRY-RUN
底层：ccxt 4.x（官方 Backpack 支持）

策略：
  现货腿永远 maker（买一价）| 合约腿初始 maker/post_only（卖一价）
  一条腿有成交后另一条立即改可成交限价追单（带滑点上限）
  追单超时（HEDGE_TIMEOUT_S）未成交 → 回滚已成交腿，避免裸敞口
  两腿都不动满 PAIR_TIMEOUT_S → 撤单重挂，最多 MAX_RETRIES 次

v5.0 改动（按外部代码安全审计修复 P0/P1）：
  ★ 追单/平仓价格方向修正：可成交限价（cross price + 滑点上限），不再反挂
  ★ 永续平仓强制 reduceOnly，杜绝反向开仓
  ★ 合约持仓按方向识别（signed contracts × contractSize 换算基础资产）
  ★ OrderNotFound 不再伪造成交：状态分 open/closed/canceled/unknown，
    unknown 冻结该币种并提示人工处理
  ★ 撤单后重新确认最终成交量；所有记账按"已确认成交增量"驱动，杜绝漏记/重复
  ★ SQLite 轻量账本（orders/fills/strategy_positions/tasks），重启可恢复
  ★ 借贷按意图区分：仅开仓买入允许 autoBorrow（借 USDC）；平仓卖现货
    禁止借入标的币，允许赎回出借；平仓后验证 USDC 债务归零
  ★ 平仓只关闭账本记录的策略持仓，不动人工持仓；启动对账发现未知敞口禁止开仓
  ★ 费率硬门槛：费率为负或净年化（含成本缓冲）不达标拒绝开仓，按实际结算周期年化
  ★ leverage 真正参与目标名义计算（target_notional = notional × leverage）
  ★ 服务只监听 127.0.0.1；POST 接口需 X-Auth-Token；实盘缺 key/缺 token 拒绝启动
  ★ /api/open、/api/close 改为任务模式，后台结果可查询（/api/task/<id>）
  ★ 交易错误响应脱敏，不向前端返回原始交易所错误
"""
import hmac
import logging
import math
import os
import random
import secrets
import sqlite3
import sys
import threading
import time
from datetime import datetime
from functools import wraps
from typing import Dict, List, Optional, Tuple

import ccxt
from flask import Flask, jsonify, redirect, render_template, request, session

# =====================================================================
# 配置 — 沿用现有策略参数（含 v5.0 安全项）
# =====================================================================
PORT = 5055
HOST = "127.0.0.1"              # ★ v5.0: 只监听本机，不再 0.0.0.0
VERSION = "5.1.3"               # ★ 页面/API 展示的当前版本号

# ★ 先加载 .env，再读配置（否则 .env 里的 BPX_LIVE=1 拿不到）
env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(env_file):
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ[k] = v

DRY_RUN = os.environ.get("BPX_LIVE", "0").strip() != "1"
ORDER_SIZE_USDC = 100.0            # 单笔名义 USDC
PAIR_TIMEOUT_S = 60                # 单对 maker 等待超时（原 180s，缩短裸露窗口）
HEDGE_TIMEOUT_S = 5                # ★ 追单/回滚等待超时（审计建议 2-5s）
MAX_RETRIES = 3                    # 最大重挂次数
MIN_NET_APY = 10.0                 # ★ 费率硬门槛：年化低于此值拒绝开仓（原 MIN_WEEK_APY）
EST_ROUND_TRIP_COST_APY = 5.0      # ★ 往返成本缓冲（手续费+滑点+基差）：净年化 ≤ 0 时拒绝开仓
MAX_SLIPPAGE_BPS = 20              # ★ 追单/平仓可成交限价的滑点上限（0.2%）
FUNDING_INTERVAL_DEFAULT_S = 3600  # ★ 资金费结算周期缺省值（Backpack 当前为小时级）
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "arb_ledger.db")
BPX_PUBLIC_KEY = os.environ.get("BPX_PUBLIC_KEY", "")
BPX_SECRET_KEY = os.environ.get("BPX_SECRET_KEY", "")
# ★ v5.1: 网页登录保护（用户名/密码），替代旧 BPX_WEB_TOKEN（token 注入页面=谁都能拿）
BPX_WEB_USER = os.environ.get("BPX_WEB_USER", "").strip()
BPX_WEB_PASSWORD = os.environ.get("BPX_WEB_PASSWORD", "").strip()
# ★ 可选代理（如 CLI 直连被墙、浏览器走系统代理时）:
#   在 .env 里配置 BPX_PROXY=http://127.0.0.1:10808 即可（ccxt 不读环境变量代理，必须显式设置）
BPX_PROXY = os.environ.get("BPX_PROXY", "").strip()
if BPX_PROXY and not BPX_PROXY.startswith("http://") and not BPX_PROXY.startswith("https://"):
    BPX_PROXY = f"http://{BPX_PROXY}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("bpx_arb.log", encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger("arb")

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)   # ★ 会话 cookie 签名密钥（每次启动更换，旧会话失效）

# =====================================================================
# 初始化 ccxt + 启动硬校验
# =====================================================================
has_key = bool(BPX_PUBLIC_KEY and BPX_SECRET_KEY)
ex: ccxt.backpack = ccxt.backpack({
    "apiKey": BPX_PUBLIC_KEY,
    "secret": BPX_SECRET_KEY,
    "enableRateLimit": True,
})
if BPX_PROXY:
    ex.proxies = {"http": BPX_PROXY, "https": BPX_PROXY}
    logger.info("已启用 HTTP 代理: %s", BPX_PROXY)
logger.info("ccxt %s 已初始化 | %s", ccxt.__version__, "DRY-RUN" if DRY_RUN else "实盘")

# ★ 启动硬校验
#   1. 实盘缺 API key → 拒绝启动（不再假跑 dry-run）
#   2. 缺登录凭据 → 拒绝启动（公网可操作=裸奔）
if not DRY_RUN and not has_key:
    logger.error("实盘模式(BPX_LIVE=1)必须配置 BPX_PUBLIC_KEY/BPX_SECRET_KEY，拒绝启动")
    sys.exit(1)
if not DRY_RUN and not (BPX_WEB_USER and BPX_WEB_PASSWORD):
    logger.error("实盘模式(BPX_LIVE=1)必须配置 BPX_WEB_USER/BPX_WEB_PASSWORD（网页登录保护），拒绝启动")
    sys.exit(1)
if DRY_RUN and not (BPX_WEB_USER and BPX_WEB_PASSWORD):
    BPX_WEB_USER = "admin"
    BPX_WEB_PASSWORD = secrets.token_urlsafe(12)
    logger.info("DRY-RUN 未配置登录凭据，已自动生成 → 用户名: %s  密码: %s（重启后失效）",
                BPX_WEB_USER, BPX_WEB_PASSWORD)

# 预加载市场（精度/符号/过滤器）
try:
    markets = ex.load_markets()
    spot_symbols = [s for s in markets if markets[s]["spot"]]
    perp_symbols = [s for s in markets if markets[s]["swap"]]
    logger.info("市场加载完成: 现货 %d 永续 %d", len(spot_symbols), len(perp_symbols))
except Exception as e:
    logger.error("加载市场失败: %s", e)
    markets = {}
    spot_symbols = []
    perp_symbols = []

# =====================================================================
# 全局状态
# =====================================================================
active_orders: Dict[str, List[dict]] = {}    # {base_coin: [order_info, ...]}
operation_log: List[str] = []
_trade_lock = threading.Lock()
_inflight: set = set()
_frozen: set = set()                         # ★ 订单状态未知被冻结的币种
_exposed: set = set()                        # ★ 回滚失败的裸敞口币种（EXPOSED）
_unknown_exposure = False                    # ★ 启动对账发现未知敞口
_cache_lock = threading.Lock()
_funding_rate_cache: Dict[str, tuple] = {}   # {sym: (apy, timestamp)} TTL 60s
_task_results: Dict[str, dict] = {}          # {task_id: {status, result}}
_dry_seq = 0
_dry_seq_lock = threading.Lock()


def add_log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    operation_log.append(entry)
    if len(operation_log) > 200:
        operation_log.pop(0)
    logger.info(msg)


# =====================================================================
# SQLite 轻量账本（v5.0 新增）
#   账本只做两件事：记录订单生命周期 + 按"已确认成交增量"维护策略持仓。
#   任何仓位变化只能由 fetch_order 确认的增量驱动，不能由订单状态推断。
# =====================================================================

def _db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    conn = _db_conn()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          client_order_id TEXT UNIQUE,
          order_id TEXT,
          symbol TEXT NOT NULL,
          market TEXT NOT NULL,
          side TEXT NOT NULL,
          intent TEXT NOT NULL,
          requested_amount REAL NOT NULL,
          price REAL,
          status TEXT DEFAULT 'new',
          last_confirmed_filled REAL DEFAULT 0,
          reduce_only INTEGER DEFAULT 0,
          created_at TEXT,
          updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS fills (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          order_id TEXT,
          client_order_id TEXT,
          symbol TEXT,
          market TEXT,
          side TEXT,
          qty REAL NOT NULL,
          price REAL,
          ts TEXT
        );
        CREATE TABLE IF NOT EXISTS strategy_positions (
          symbol TEXT PRIMARY KEY,
          spot_qty REAL DEFAULT 0,
          perp_qty REAL DEFAULT 0,
          updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS tasks (
          id TEXT PRIMARY KEY,
          action TEXT,
          symbol TEXT,
          status TEXT,
          result TEXT,
          created_at TEXT,
          updated_at TEXT
        );
        """)
        conn.commit()
    finally:
        conn.close()


def _gen_client_order_id(symbol: str, intent: str) -> str:
    """★ 生成 Backpack 兼容的 clientId（交易所要求 uint32 整数，字符串会被 400 拒绝）。
    以十进制字符串存储于账本，碰撞时重试。"""
    for _ in range(8):
        cid = str(random.randint(1, 0xFFFFFFFF))
        if not _get_order_row(cid):
            return cid
    return str(int(time.time() * 1000) % 0xFFFFFFFF)


def _record_order(symbol: str, market: str, side: str, intent: str,
                  amount: float, price: float, client_order_id: str = None,
                  reduce_only: bool = False) -> str:
    """登记订单（下单前调用），返回 client_order_id"""
    if not client_order_id:
        client_order_id = _gen_client_order_id(symbol, intent)
    now = datetime.now().isoformat()
    conn = _db_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO orders (client_order_id, symbol, market, side, intent, "
            "requested_amount, price, status, last_confirmed_filled, reduce_only, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,'new',0,?,?,?)",
            (client_order_id, symbol, market, side, intent, amount, price,
             1 if reduce_only else 0, now, now))
        conn.commit()
    finally:
        conn.close()
    return client_order_id


def _bind_order_id(client_order_id: str, order_id: str):
    """下单返回后回填交易所 order_id"""
    conn = _db_conn()
    try:
        conn.execute("UPDATE orders SET order_id=?, status='open', updated_at=? WHERE client_order_id=?",
                     (order_id, datetime.now().isoformat(), client_order_id))
        conn.commit()
    finally:
        conn.close()


def _get_order_row(order_id_or_client_id: str) -> Optional[dict]:
    """按 order_id 或 client_order_id 查订单行（两者任一均可定位）"""
    conn = _db_conn()
    try:
        row = conn.execute(
            "SELECT * FROM orders WHERE client_order_id=? OR order_id=?",
            (order_id_or_client_id, order_id_or_client_id)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _update_order_fill(symbol: str, market: str, side: str, order_id: str,
                       client_order_id: str, confirmed_filled: float, status: str):
    """★ 核心记账：按已确认增量更新账本与策略持仓。
    delta = 本次确认累计成交量 - 上次已确认累计成交量，仅 delta>0 记账。"""
    conn = _db_conn()
    try:
        row = conn.execute("SELECT last_confirmed_filled FROM orders WHERE client_order_id=?",
                           (client_order_id,)).fetchone()
        last = float(row["last_confirmed_filled"]) if row else 0.0
        now = datetime.now().isoformat()
        if status in ("open", "closed", "canceled"):
            conn.execute(
                "UPDATE orders SET status=?, order_id=COALESCE(?, order_id), "
                "last_confirmed_filled=?, updated_at=? WHERE client_order_id=?",
                (status, order_id, confirmed_filled, now, client_order_id))
        delta = round(confirmed_filled - last, 12)
        if delta > 1e-10:
            conn.execute(
                "INSERT INTO fills (order_id, client_order_id, symbol, market, side, qty, price, ts) "
                "VALUES (?,?,?,?,?,?,0,?)",
                (order_id, client_order_id, symbol, market, side, delta, now))
            # 持仓方向: spot buy + / sell - ; perp sell(开空) - / buy(平多) +
            sign = 1.0 if side == "buy" else -1.0
            pos = conn.execute("SELECT spot_qty, perp_qty FROM strategy_positions WHERE symbol=?",
                               (symbol,)).fetchone()
            s_q = float(pos["spot_qty"]) if pos else 0.0
            p_q = float(pos["perp_qty"]) if pos else 0.0
            if market == "spot":
                s_q = round(s_q + delta * sign, 12)
            else:
                p_q = round(p_q + delta * sign, 12)
            conn.execute(
                "INSERT INTO strategy_positions (symbol, spot_qty, perp_qty, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(symbol) DO UPDATE SET spot_qty=excluded.spot_qty, "
                "perp_qty=excluded.perp_qty, updated_at=excluded.updated_at",
                (symbol, s_q, p_q, now))
        conn.commit()
    finally:
        conn.close()


def _get_strategy_position(symbol: str) -> dict:
    """读取账本策略持仓。perp_qty 为带符号基础资产量（空头为负）。"""
    conn = _db_conn()
    try:
        row = conn.execute("SELECT spot_qty, perp_qty FROM strategy_positions WHERE symbol=?",
                           (symbol,)).fetchone()
        if row:
            return {"spot_qty": float(row["spot_qty"]), "perp_qty": float(row["perp_qty"])}
        return {"spot_qty": 0.0, "perp_qty": 0.0}
    finally:
        conn.close()


def _all_strategy_positions() -> List[dict]:
    conn = _db_conn()
    try:
        rows = conn.execute("SELECT symbol, spot_qty, perp_qty FROM strategy_positions "
                            "WHERE ABS(spot_qty) > 1e-8 OR ABS(perp_qty) > 1e-8").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# =====================================================================
# ccxt 辅助
# =====================================================================

def _ccxt_spot(symbol: str) -> str:
    """将币种代码转为 ccxt 现货格式: MON → MON/USDC"""
    return f"{symbol}/USDC"


def _ccxt_perp(symbol: str) -> str:
    """将币种代码转为 ccxt 永续格式: MON → MON/USDC:USDC"""
    return f"{symbol}/USDC:USDC"


def _symbol_from_ccxt(ccxt_sym: str) -> str:
    """ccxt 格式转回币种代码: MON/USDC:USDC → MON"""
    return ccxt_sym.split("/")[0]


def _market_of(ccxt_sym: str) -> str:
    return "perp" if ":USDC" in ccxt_sym else "spot"


def _contract_size(ccxt_sym: str) -> float:
    """★ 从市场定义读取合约乘数，不默认 1 张 = 1 币"""
    try:
        m = markets.get(ccxt_sym) or {}
        cs = m.get("contractSize") or 1
        return float(cs)
    except Exception:
        return 1.0


def _depth_bbo(ccxt_sym: str) -> tuple:
    """返回 (买一价, 卖一价)。ccxt 已自动归一 bids 为降序，bids[0]=买一"""
    try:
        ob = ex.fetch_order_book(ccxt_sym, limit=5)
        if ob["bids"] and ob["asks"]:
            return ob["bids"][0][0], ob["asks"][0][0]
    except Exception:
        pass
    return None, None


def _depth_mid(ccxt_sym: str) -> Optional[float]:
    try:
        ob = ex.fetch_order_book(ccxt_sym, limit=5)
        if ob["bids"] and ob["asks"]:
            return (ob["bids"][0][0] + ob["asks"][0][0]) / 2
    except Exception:
        pass
    return None


def _cross_price(ccxt_sym: str, side: str, max_slippage_bps: int = MAX_SLIPPAGE_BPS) -> Optional[float]:
    """★ 可成交限价（带滑点上限）：买入用卖一×(1+滑点)，卖出用买一×(1-滑点）。
    post_only=False 只代表允许吃单，限价本身必须穿过盘口才会成交。"""
    bid, ask = _depth_bbo(ccxt_sym)
    if not bid or not ask:
        return None
    slip = max_slippage_bps / 10_000.0
    if side == "buy":
        return ask * (1 + slip)
    return bid * (1 - slip)


def _get_collateral_data(exchange) -> dict:
    """拉取抵押品数据（implicit API：含借贷明细）"""
    try:
        return exchange.privateGetApiV1CapitalCollateral() or {}
    except Exception:
        return {}


def _get_max_leverage(symbol: str) -> int:
    """从市场数据取杠杆上限"""
    try:
        col = ex.publicGetApiV1Collateral()
        for c in col:
            if c.get("symbol") == symbol:
                imf = float(c.get("imfFunction", {}).get("base", 1))
                if 0 < imf < 100:
                    return math.floor(1 / imf)
    except Exception:
        pass
    return 0


def _get_haircut(symbol: str) -> float:
    """抵押品权重（base 在 kind 里）"""
    try:
        col = ex.publicGetApiV1Collateral()
        for c in col:
            if c.get("symbol") == symbol:
                kind = c.get("haircutFunction", {}).get("kind", {})
                return float(kind.get("base", kind.get("weight", 0)))
    except Exception:
        pass
    return 0


def _funding_rate_info(ccxt_sym: str) -> Tuple[Optional[float], Optional[float]]:
    """返回 (最新费率, 年化APY)。★ APY 按实际结算周期年化，不再硬编码每小时。"""
    try:
        fr = ex.fetch_funding_rate(ccxt_sym)
        rate = float(fr.get("fundingRate", 0) or 0)
        interval = float(fr.get("fundingInterval") or markets.get(ccxt_sym, {}).get("fundingInterval")
                         or FUNDING_INTERVAL_DEFAULT_S)
        if interval <= 0:
            interval = FUNDING_INTERVAL_DEFAULT_S
        apy = rate * (86400.0 / interval) * 365.0 * 100.0
        return rate, apy
    except Exception:
        return None, None


def _get_spot_total(symbol: str) -> Optional[float]:
    """★ 账户该币种实际总持有量（含出借中）。
    collateral.totalQuantity 对部分币种（如 XPL）不包含出借量：125 买入全出借后
    totalQuantity 只显示 1.0073。取 collateral 汇总与 borrowLend positions 的最大兜底。"""
    if not has_key or DRY_RUN:
        return None
    total = 0.0
    try:
        col = _get_collateral_data(ex)
        for ci in col.get("collateral", []) or []:
            if ci.get("symbol") == symbol:
                total = max(total, float(ci.get("totalQuantity", 0) or 0))
                total = max(total,
                            float(ci.get("lendQuantity", 0) or 0)
                            + float(ci.get("availableQuantity", 0) or 0)
                            + float(ci.get("openOrderQuantity", 0) or 0))
    except Exception:
        pass
    try:
        for p in (ex.privateGetApiV1BorrowLendPositions() or []):
            if p.get("symbol") == symbol:
                total = max(total, float(p.get("netQuantity", 0) or 0))
    except Exception:
        pass
    return total if total > 0 else None


def _get_usdc_borrow() -> Optional[float]:
    """★ 读取账户借款余额（Backpack 实际格式: collateral 顶层字段 borrowLiability）。
    平仓后必须验证债务归零，避免自以为平仓成功。取不到字段返回 None（未知）。"""
    if not has_key or DRY_RUN:
        return None
    try:
        col = _get_collateral_data(ex)
        if "borrowLiability" in col:
            return float(col.get("borrowLiability", 0) or 0)
        if "liabilitiesValue" in col:
            return float(col.get("liabilitiesValue", 0) or 0)
        # 兜底: 逐币种字段
        for ci in col.get("collateral", []) or []:
            if ci.get("symbol") == "USDC":
                for k in ("borrowQuantity", "borrowedQuantity", "borrow", "liabilityQuantity"):
                    if k in ci:
                        return float(ci[k] or 0)
        return None
    except Exception:
        return None


# =====================================================================
# 下单 / 撤单 / 查单（v5.0 重写）
# =====================================================================

def _order_params(market: str, intent: str, side: str, reduce_only: bool = False) -> dict:
    """★ 按交易意图生成交易所参数。
    借贷规则（Backpack spot margin 参数，见 ccxt backpack createOrder 文档）：
      - 开仓买入现货: autoBorrow（借 USDC）+ autoLend（买到的币自动出借）
      - 平仓卖出现货: autoLendRedeem（赎回出借的币）+ autoBorrowRepay（卖出所得自动归还
        USDC 借款）+ 禁止 autoBorrow 借入标的币
      - 永续平仓: 强制 reduceOnly，禁止反向开仓"""
    params: dict = {}
    if market == "spot":
        if intent == "open" and side == "buy":
            params["autoBorrow"] = True
            params["autoLend"] = True
            params["autoLendRedeem"] = True   # ★ 可赎回已出借资产：账户 USDC 全被借出时，
            #                                    买入借币会报 BORROW_REQUIRES_LEND_REDEEM
        elif intent == "close" and side == "sell":
            params["autoLend"] = True
            params["autoLendRedeem"] = True
            params["autoBorrowRepay"] = True   # ★ 卖出所得自动归还 USDC 借款
    else:
        if reduce_only:
            params["reduceOnly"] = True
    return params


def _rescue_unknown_placement(ccxt_sym: str, client_order_id: str) -> Optional[str]:
    """★ 下单请求网络异常后，订单可能已被交易所接受。
    用 client_id 扫描未成交订单找回，避免重复下单。"""
    if client_order_id.startswith("dry-"):
        return None
    try:
        for o in (ex.fetch_open_orders(ccxt_sym) or []):
            if str(o.get("clientOrderId", "") or o.get("clientId", "") or "") == client_order_id:
                return str(o["id"])
    except Exception:
        pass
    return None


def _place_limit(exchange, ccxt_sym: str, side: str, amount: float,
                 price: float, intent: str = "open", post_only: bool = True,
                 reduce_only: bool = False) -> Tuple[Optional[str], Optional[str]]:
    """下限价单。返回 (order_id, client_order_id)。失败返回 (None, None)。
    精度由 ccxt 的 amount_to_precision / price_to_precision 自动处理"""
    symbol = _symbol_from_ccxt(ccxt_sym)
    market = _market_of(ccxt_sym)
    action = f"{'maker' if post_only else 'taker'} {side} {ccxt_sym} {amount:.4f} @ {price}"
    coid = _record_order(symbol, market, side, intent, amount, price, reduce_only=reduce_only)

    if DRY_RUN or not has_key:
        with _dry_seq_lock:
            global _dry_seq
            _dry_seq += 1
            dry_id = f"dry-{int(time.time() * 1000)}-{_dry_seq}"
        conn = _db_conn()
        try:
            conn.execute("UPDATE orders SET order_id=?, status='open', updated_at=? WHERE client_order_id=?",
                         (dry_id, datetime.now().isoformat(), coid))
            conn.commit()
        finally:
            conn.close()
        add_log(f"[DRY] {action}")
        return dry_id, coid

    try:
        params = _order_params(market, intent, side, reduce_only)
        params["clientOrderId"] = int(coid)   # ★ ccxt 文档参数：自动转 uint32 的 clientId
        if post_only:
            params["postOnly"] = True
        order = exchange.create_order(
            symbol=ccxt_sym,
            type="limit",
            side=side,
            amount=amount,
            price=price,
            params=params,
        )
        oid = order.get("id")
        if oid:
            _bind_order_id(coid, str(oid))
            add_log(f"{action} → order_id={oid}")
            return str(oid), coid
        add_log(f"[FAIL] {action}: 交易所未返回订单号 order={order}")
    except ccxt.InvalidOrder as e:
        add_log(f"[FAIL] InvalidOrder {action}: {str(e)[:120]}")
    except ccxt.InsufficientFunds as e:
        add_log(f"[FAIL] 余额不足 {action}: {str(e)[:120]}")
    except ccxt.NetworkError as e:
        # ★ 网络异常时订单可能已被接受：尝试找回，找回失败提示人工核查
        err_txt = str(e)
        add_log(f"[FAIL] 网络异常 {action}: {err_txt}")
        rescued = _rescue_unknown_placement(ccxt_sym, coid)
        if rescued:
            _bind_order_id(coid, rescued)
            add_log(f"[恢复] 下单网络异常但已找回订单 {rescued}")
            return rescued, coid
        if "400" in err_txt or "Bad Request" in err_txt or "parse request" in err_txt:
            add_log(f"[FAIL] 下单被交易所拒绝（400），订单未接受，无需人工核查 {coid}")
        else:
            add_log(f"[警告] 下单请求状态未知，可能已被交易所接受，请人工核查 {coid}")
    except Exception as e:
        add_log(f"[FAIL] {action}: {type(e).__name__} {str(e)[:120]}")
    return None, coid


def _check_order_filled(exchange, ccxt_sym: str, order_id: str) -> Tuple[str, float, float]:
    """★ 返回 (status, filled, avg_price)。
    status ∈ {open, closed, canceled, unknown}。
    绝不把 OrderNotFound 当成已成交 —— 状态未知就是未知（unknown）。"""
    if order_id.startswith("dry-"):
        row = _get_order_row(order_id)
        return "closed", float(row["requested_amount"]) if row else 0.0, 0.0
    if DRY_RUN or not has_key:
        return "closed", 0.0, 0.0

    # ★ 路径1: 交易所支持 fetchOrder（ccxt.backpack 不支持！返回 NotSupported）
    if exchange.has.get("fetchOrder"):
        def _read():
            order = exchange.fetch_order(order_id, ccxt_sym)
            status = order.get("status", "")
            filled = float(order.get("filled", 0) or 0)
            amount = float(order.get("amount", 0) or 0)
            price = float(order.get("average", 0) or order.get("price", 0) or 0)
            if status == "closed":
                return "closed", filled, price
            if status == "canceled":
                return "canceled", filled, price
            if status == "open" and amount > 0 and filled >= amount:
                return "closed", filled, price
            return "open", filled, price

        try:
            return _read()
        except ccxt.OrderNotFound:
            # 查不到：可能已成交归档/已撤销/节点不同步。短暂重试一次，仍未知则 UNKNOWN。
            try:
                time.sleep(1)
                return _read()
            except Exception:
                add_log(f"[UNKNOWN] 订单 {order_id} 查询不到，状态未知（不假定成交，不重发同单）")
                return "unknown", 0.0, 0.0
        except ccxt.NetworkError as e:
            # 网络抖动：视为仍挂单，下一轮继续查
            add_log(f"[警告] 查单网络异常 {order_id}: {e}")
            return "open", 0.0, 0.0
        except Exception as e:
            add_log(f"[警告] 查单异常 {order_id}: {type(e).__name__} {str(e)[:80]}")
            return "open", 0.0, 0.0

    # ★ 路径2: backpack 等不支持 fetchOrder → 三级对账（未成交 → 订单历史 → 成交记录）
    # 2a. 未成交订单列表（部分成交也在此，filled 即已成交量）
    try:
        opens = exchange.fetch_open_orders(ccxt_sym) or []
        for o in opens:
            if str(o.get("id")) == str(order_id):
                filled = float(o.get("filled", 0) or 0)
                amount = float(o.get("amount", 0) or 0)
                if amount > 0 and filled >= amount:
                    return "closed", filled, 0.0
                return "open", filled, 0.0
    except Exception as e:
        add_log(f"[警告] 查未成交订单异常 {order_id}: {type(e).__name__} {str(e)[:80]}")
        return "open", 0.0, 0.0

    # 2b. 不在未成交列表 → 查订单历史确认最终状态与成交量
    try:
        orders = exchange.fetch_orders(ccxt_sym) or []
        for o in orders:
            if str(o.get("id")) == str(order_id):
                status = o.get("status", "")
                filled = float(o.get("filled", 0) or 0)
                amount = float(o.get("amount", 0) or 0)
                if status == "closed":
                    return "closed", filled, 0.0
                if status == "open":
                    return "open", filled, 0.0
                return "canceled", filled, 0.0
    except Exception as e:
        add_log(f"[警告] 查订单历史异常 {order_id}: {type(e).__name__} {str(e)[:80]}")

    # 2c. 历史也没有 → 用成交记录兜底（部分交易所成交后订单立即归档）
    try:
        trades = exchange.fetch_my_trades(ccxt_sym) or []
        total = 0.0
        for t in trades:
            if str(t.get("order") or "") == str(order_id):
                total += float(t.get("amount", 0) or 0)
        if total > 0:
            return "closed", total, 0.0
    except Exception as e:
        add_log(f"[警告] 查成交记录异常 {order_id}: {type(e).__name__} {str(e)[:80]}")

    add_log(f"[UNKNOWN] 订单 {order_id} 无法确认状态（不假定成交，不重发同单）")
    return "unknown", 0.0, 0.0


def _confirm_final_fill(ccxt_sym: str, order_id: str, client_order_id: str,
                        symbol: str, market: str, side: str):
    """★ 撤单后重新确认最终成交量，并以增量记账（撤单与成交竞态防护）"""
    for _ in range(2):
        status, filled, price = _check_order_filled(ex, ccxt_sym, order_id)
        _update_order_fill(symbol, market, side, order_id, client_order_id, filled, status)
        if status in ("closed", "canceled", "unknown"):
            return status, filled
        time.sleep(1)
    return status, filled


def _cancel_order(ccxt_sym: str, order_id: str, client_order_id: str,
                  symbol: str, market: str, side: str) -> bool:
    """撤单并重确认最终成交量。返回订单是否已不在挂单（撤单成功或订单已关闭）。"""
    if order_id.startswith("dry-"):
        add_log(f"[DRY] 撤单 {ccxt_sym} {order_id}")
        _confirm_final_fill(ccxt_sym, order_id, client_order_id, symbol, market, side)
        return True
    try:
        ex.cancel_order(order_id, ccxt_sym)
        add_log(f"撤单 {ccxt_sym} {order_id}")
    except ccxt.OrderNotFound:
        add_log(f"撤单 {ccxt_sym} {order_id}: 订单已不存在（可能已成交），重新确认最终量")
    except ccxt.BadRequest as e:
        # backpack 对已关闭订单撤单返回 BadRequest("Order not found")，等同 OrderNotFound
        if "not found" in str(e).lower() or "INVALID_CLIENT_REQUEST" in str(e):
            add_log(f"撤单 {ccxt_sym} {order_id}: 订单已不存在（可能已成交），重新确认最终量")
        else:
            add_log(f"[FAIL] 撤单 {ccxt_sym} {order_id}: {str(e)[:120]}")
            return False
    except Exception as e:
        add_log(f"[FAIL] 撤单 {ccxt_sym} {order_id}: {type(e).__name__} {str(e)[:120]}")
        return False
    _confirm_final_fill(ccxt_sym, order_id, client_order_id, symbol, market, side)
    return True


# =====================================================================
# 交易逻辑（v5.0 重写）
# =====================================================================

def _freeze(symbol: str, reason: str):
    _frozen.add(symbol)
    add_log(f"⛔ [{symbol}] 已冻结: {reason}（需人工核查，重启或人工处理后恢复）")


def _mark_exposed(symbol: str, reason: str):
    _exposed.add(symbol)
    add_log(f"⚠ EXPOSED [{symbol}]: {reason} 存在单边敞口，请尽快人工处理！")


def _spot_done(symbol: str, before: float) -> float:
    """本次开仓对已确认的现货买入量（账本增量驱动）"""
    now = _get_strategy_position(symbol)
    return max(0.0, now["spot_qty"] - before)


def _perp_done(symbol: str, before: float) -> float:
    """本次开仓对已确认的永续开空量（账本增量驱动，空头方向）"""
    now = _get_strategy_position(symbol)
    return max(0.0, before - now["perp_qty"])


def _rollback_open_leg(symbol: str, market: str, qty: float) -> bool:
    """★ 开仓失败回滚已成交腿：现货→卖出；永续→买入平仓(reduceOnly)。
    全部走滑点上限内的可成交限价。失败标记 EXPOSED。"""
    if qty <= 1e-8:
        return True
    ccxt_sym = _ccxt_spot(symbol) if market == "spot" else _ccxt_perp(symbol)
    side = "sell" if market == "spot" else "buy"
    px = _cross_price(ccxt_sym, side)
    if px is None:
        _mark_exposed(symbol, f"回滚{market}腿无盘口")
        return False
    add_log(f"  ⚠ [{symbol}] 回滚{market}腿 {qty:.4f} @ {px:.6f}")
    oid, coid = _place_limit(ex, ccxt_sym, side, qty, px, intent="close",
                             post_only=False, reduce_only=(market == "perp"))
    if not oid:
        _mark_exposed(symbol, f"回滚{market}腿下单失败")
        return False
    t0 = time.time()
    while time.time() - t0 < HEDGE_TIMEOUT_S:
        time.sleep(1)
        status, filled, _ = _check_order_filled(ex, ccxt_sym, oid)
        _update_order_fill(symbol, market, side, oid, coid, filled, status)
        if status == "closed":
            add_log(f"  [{symbol}] 回滚{market}腿完成 ✓")
            return True
        if status == "unknown":
            _freeze(symbol, "回滚订单状态未知")
            _mark_exposed(symbol, f"回滚{market}腿状态未知")
            return False
    _cancel_order(ccxt_sym, oid, coid, symbol, market, side)
    row = _get_order_row(coid)
    rolled = float(row["last_confirmed_filled"]) if row else 0.0
    remaining = max(0.0, qty - rolled)
    _mark_exposed(symbol, f"回滚{market}腿未成交(剩{remaining:.4f})")
    return False


def execute_pair(symbol: str, qty: float, timeout_s: int = PAIR_TIMEOUT_S) -> Tuple[bool, float, float, str]:
    """★ 执行一对：现货买 + 合约卖（开仓）。
    返回 (是否成功, spot_filled, perp_filled, 说明)。
    关键修复：
      - 任一腿有成交即立即追单（不等整腿成交，杜绝长时间裸露）
      - 追单价格可成交（买一/卖一对侧 + 滑点上限）
      - 追单超时回滚已成交腿
      - 全部记账由账本"已确认增量"驱动"""
    spot_sym = _ccxt_spot(symbol)
    perp_sym = _ccxt_perp(symbol)

    spot_bid, spot_ask = _depth_bbo(spot_sym)
    perp_bid, perp_ask = _depth_bbo(perp_sym)
    if not spot_bid or not spot_ask or not perp_bid or not perp_ask:
        add_log(f"[跳] {symbol} 无盘口数据")
        return False, 0.0, 0.0, "无盘口数据"

    for attempt in range(1, MAX_RETRIES + 1):
        before = _get_strategy_position(symbol)
        spot_before = before["spot_qty"]
        perp_before = before["perp_qty"]
        s_done = _spot_done(symbol, spot_before)
        p_done = _perp_done(symbol, perp_before)
        spot_amount = qty - s_done
        perp_amount = qty - p_done
        add_log(f"  [{symbol}] 第{attempt}次: 现货买@{spot_bid:.6f}({spot_amount:.4f}个) "
                f"合约卖@{perp_ask:.6f}({perp_amount:.4f}个)")

        spot_oid = perp_oid = None
        spot_coid = perp_coid = None
        if spot_amount > 1e-8:
            spot_oid, spot_coid = _place_limit(ex, spot_sym, "buy", spot_amount, spot_bid, intent="open", post_only=True)
        if perp_amount > 1e-8:
            perp_oid, perp_coid = _place_limit(ex, perp_sym, "sell", perp_amount, perp_ask, intent="open", post_only=True)

        # 任一腿下单失败：撤另一腿（带重确认），如实返回已确认量
        if (spot_amount > 1e-8 and not spot_oid) or (perp_amount > 1e-8 and not perp_oid):
            if spot_oid:
                _cancel_order(spot_sym, spot_oid, spot_coid, symbol, "spot", "buy")
            if perp_oid:
                _cancel_order(perp_sym, perp_oid, perp_coid, symbol, "perp", "sell")
            return False, _spot_done(symbol, spot_before), _perp_done(symbol, perp_before), "下单失败"
        if not spot_oid and not perp_oid:
            return True, s_done, p_done, ""  # 两腿在上轮已全成

        start = time.time()
        spot_chased = False
        perp_chased = False
        while time.time() - start < timeout_s:
            time.sleep(2)

            s_status = p_status = "open"
            if spot_oid:
                s_status, s_filled, _ = _check_order_filled(ex, spot_sym, spot_oid)
                _update_order_fill(symbol, "spot", "buy", spot_oid, spot_coid, s_filled, s_status)
            if perp_oid:
                p_status, p_filled, _ = _check_order_filled(ex, perp_sym, perp_oid)
                _update_order_fill(symbol, "perp", "sell", perp_oid, perp_coid, p_filled, p_status)

            # 订单状态未知 → 冻结该币种，不再继续下单（人工处理）
            if (spot_oid and s_status == "unknown") or (perp_oid and p_status == "unknown"):
                _freeze(symbol, "订单状态未知")
                return False, _spot_done(symbol, spot_before), _perp_done(symbol, perp_before), "订单状态未知，已冻结"

            s_done = _spot_done(symbol, spot_before)
            p_done = _perp_done(symbol, perp_before)
            s_full = s_done >= qty - 1e-8
            p_full = p_done >= qty - 1e-8
            if s_full and p_full:
                add_log(f"  [{symbol}] 两腿均成交 ✓ (累计 现{s_done:.4f}/合{p_done:.4f})")
                return True, s_done, p_done, ""

            # ★ 现货腿有成交、永续未满 → 立即追单卖永续（可成交价）
            if s_done > 1e-8 and not p_full and not perp_chased:
                perp_chased = True
                if perp_oid:
                    _cancel_order(perp_sym, perp_oid, perp_coid, symbol, "perp", "sell")
                    perp_oid = perp_coid = None
                p_done = _perp_done(symbol, perp_before)
                if p_done < qty - 1e-8:
                    remaining = qty - p_done
                    px = _cross_price(perp_sym, "sell")   # ★ 卖单用买一×(1-滑点)
                    if px is None:
                        _mark_exposed(symbol, "合约追单无盘口")
                        return False, s_done, p_done, "合约追单无盘口"
                    add_log(f"  [{symbol}] 现货成交，合约追单卖 {remaining:.4f} @ {px:.6f}")
                    perp_oid, perp_coid = _place_limit(ex, perp_sym, "sell", remaining, px,
                                                       intent="open", post_only=False)
                    if not perp_oid:
                        _rollback_open_leg(symbol, "spot", s_done)
                        return False, s_done, p_done, "合约追单失败，已回滚现货"
                    t0 = time.time()
                    while time.time() - t0 < HEDGE_TIMEOUT_S:
                        time.sleep(1)
                        pt, pf, _ = _check_order_filled(ex, perp_sym, perp_oid)
                        _update_order_fill(symbol, "perp", "sell", perp_oid, perp_coid, pf, pt)
                        if pt == "unknown":
                            _freeze(symbol, "追单订单状态未知")
                            return False, _spot_done(symbol, spot_before), _perp_done(symbol, perp_before), "追单状态未知"
                        if _perp_done(symbol, perp_before) >= qty - 1e-8:
                            break
                    p_done = _perp_done(symbol, perp_before)
                    if p_done < qty - 1e-8:
                        # ★ 追单超时: 必须先撤追单单并重确认最终量（防追单晚成交造成裸敞口），
                        #    只回滚未对冲部分，绝不能在追单仍挂着时直接回滚
                        if perp_oid:
                            _cancel_order(perp_sym, perp_oid, perp_coid, symbol, "perp", "sell")
                            perp_oid = perp_coid = None
                        p_done = _perp_done(symbol, perp_before)
                        uncovered = max(0.0, s_done - p_done)
                        if uncovered > 1e-8:
                            add_log(f"  ⚠ [{symbol}] 合约追单 {HEDGE_TIMEOUT_S}s 未成交，回滚现货 {uncovered:.4f}")
                            _rollback_open_leg(symbol, "spot", uncovered)
                        return False, s_done, p_done, "合约追单未成交，已回滚现货"

            # ★ 永续腿有成交、现货未满 → 立即追单买现货（可成交价）
            elif p_done > 1e-8 and not s_full and not spot_chased:
                spot_chased = True
                if spot_oid:
                    _cancel_order(spot_sym, spot_oid, spot_coid, symbol, "spot", "buy")
                    spot_oid = spot_coid = None
                s_done = _spot_done(symbol, spot_before)
                if s_done < qty - 1e-8:
                    remaining = qty - s_done
                    px = _cross_price(spot_sym, "buy")    # ★ 买单用卖一×(1+滑点)
                    if px is None:
                        _mark_exposed(symbol, "现货追单无盘口")
                        return False, s_done, p_done, "现货追单无盘口"
                    add_log(f"  [{symbol}] 合约成交，现货追单买 {remaining:.4f} @ {px:.6f}")
                    spot_oid, spot_coid = _place_limit(ex, spot_sym, "buy", remaining, px,
                                                       intent="open", post_only=False)
                    if not spot_oid:
                        # ★ 只回滚未对冲部分（p_done - s_done），已成交的 s_done 保持中性，绝不整腿回滚留裸多
                        uncovered = max(0.0, p_done - s_done)
                        if uncovered > 1e-8:
                            add_log(f"  ⚠ [{symbol}] 现货追单失败，回滚未对冲合约 {uncovered:.4f}")
                            _rollback_open_leg(symbol, "perp", uncovered)
                        return False, s_done, p_done, "现货追单失败，已回滚未对冲合约"
                    t0 = time.time()
                    while time.time() - t0 < HEDGE_TIMEOUT_S:
                        time.sleep(1)
                        st2, sf2, _ = _check_order_filled(ex, spot_sym, spot_oid)
                        _update_order_fill(symbol, "spot", "buy", spot_oid, spot_coid, sf2, st2)
                        if st2 == "unknown":
                            _freeze(symbol, "追单订单状态未知")
                            return False, _spot_done(symbol, spot_before), _perp_done(symbol, perp_before), "追单状态未知"
                        if _spot_done(symbol, spot_before) >= qty - 1e-8:
                            break
                    s_done = _spot_done(symbol, spot_before)
                    if s_done < qty - 1e-8:
                        # ★ 追单超时: 必须先撤追单单并重确认最终量（防追单晚成交造成裸敞口），
                        #    只回滚未对冲部分
                        if spot_oid:
                            _cancel_order(spot_sym, spot_oid, spot_coid, symbol, "spot", "buy")
                            spot_oid = spot_coid = None
                        s_done = _spot_done(symbol, spot_before)
                        uncovered = max(0.0, p_done - s_done)
                        if uncovered > 1e-8:
                            add_log(f"  ⚠ [{symbol}] 现货追单 {HEDGE_TIMEOUT_S}s 未成交，回滚合约 {uncovered:.4f}")
                            _rollback_open_leg(symbol, "perp", uncovered)
                        return False, s_done, p_done, "现货追单未成交，已回滚合约"

        # 超时：撤两腿（带重确认），记录本次成交，重挂
        if spot_oid:
            _cancel_order(spot_sym, spot_oid, spot_coid, symbol, "spot", "buy")
        if perp_oid:
            _cancel_order(perp_sym, perp_oid, perp_coid, symbol, "perp", "sell")
        s_done = _spot_done(symbol, spot_before)
        p_done = _perp_done(symbol, perp_before)
        add_log(f"  [{symbol}] 第{attempt}次超时，撤单重挂 (累计 现{s_done:.4f}/合{p_done:.4f})")
        time.sleep(2)
        spot_bid, spot_ask = _depth_bbo(spot_sym)
        perp_bid, perp_ask = _depth_bbo(perp_sym)
        if not spot_bid or not spot_ask or not perp_bid or not perp_ask:
            return False, s_done, p_done, "无盘口数据"

    add_log(f"  [{symbol}] {MAX_RETRIES}次重试均失败 (累计 现{s_done:.4f}/合{p_done:.4f})")
    return False, s_done, p_done, "重试次数用尽"


def _get_real_position(symbol: str):
    """★ 从 API 读取真实持仓（现货 + 合约 + mark price）。
    返回 (spot_qty, perp_qty_signed, perp_mark_price)。
    perp_qty_signed 为带符号基础资产量：空头为负，多头为正。
    方向识别：优先 ccxt 的 contracts 符号，再用 side 字段兜底修正。"""
    spot_qty = 0.0
    perp_qty = 0.0
    perp_mark = 0.0
    if has_key and not DRY_RUN:
        try:
            ps = ex.fetch_positions() or []
            for p in ps:
                if p["symbol"].split("/")[0] == symbol:
                    contracts = float(p.get("contracts", 0) or 0)
                    cs = _contract_size(p["symbol"])
                    perp_qty = contracts * cs
                    side = p.get("side")
                    if side == "short" and perp_qty > 0:
                        perp_qty = -perp_qty
                    elif side == "long" and perp_qty < 0:
                        perp_qty = -perp_qty
                    perp_mark = float(p.get("markPrice", 0) or p.get("entryPrice", 0) or 0)
                    break
        except Exception:
            pass
        try:
            col = _get_collateral_data(ex)
            for ci in col.get("collateral", []) or []:
                if ci.get("symbol") == symbol:
                    spot_qty = float(ci.get("totalQuantity", 0) or 0)
                    break
        except Exception:
            pass
    return round(spot_qty, 8), round(perp_qty, 8), round(perp_mark, 8)


def close_pair(symbol: str, spot_qty: float, perp_qty: float) -> Tuple[bool, float, float]:
    """★ 平仓一对：现货卖 + 合约买入平仓。
    现货卖出：可成交价（买一×(1-滑点)），intent=close 禁止借入标的币
    合约买入：可成交价（卖一×(1+滑点)）+ reduceOnly 防反向开仓
    返回 (是否完全成交, spot_filled, perp_filled)"""
    spot_sym = _ccxt_spot(symbol)
    perp_sym = _ccxt_perp(symbol)

    has_spot = spot_qty > 1e-8
    has_perp = perp_qty > 1e-8

    spot_oid = perp_oid = None
    spot_coid = perp_coid = None
    if has_spot:
        px = _cross_price(spot_sym, "sell")
        if px is None:
            add_log(f"  [{symbol}] 现货平仓无盘口")
            return False, 0.0, 0.0
        spot_oid, spot_coid = _place_limit(ex, spot_sym, "sell", spot_qty, px, intent="close", post_only=False)
    if has_perp:
        px = _cross_price(perp_sym, "buy")
        if px is None:
            if spot_oid:
                _cancel_order(spot_sym, spot_oid, spot_coid, symbol, "spot", "sell")
            add_log(f"  [{symbol}] 合约平仓无盘口")
            return False, 0.0, 0.0
        perp_oid, perp_coid = _place_limit(ex, perp_sym, "buy", perp_qty, px,
                                           intent="close", post_only=False, reduce_only=True)

    if has_spot and not spot_oid:
        if perp_oid:
            _cancel_order(perp_sym, perp_oid, perp_coid, symbol, "perp", "buy")
        add_log(f"  [{symbol}] 现货平仓下单失败")
        return False, 0.0, 0.0
    if has_perp and not perp_oid:
        if spot_oid:
            _cancel_order(spot_sym, spot_oid, spot_coid, symbol, "spot", "sell")
        add_log(f"  [{symbol}] 合约平仓下单失败")
        return False, 0.0, 0.0

    if not spot_oid and not perp_oid:
        return True, 0.0, 0.0

    s_done = not has_spot
    p_done = not has_perp
    s_exec = 0.0
    p_exec = 0.0
    s_replaced = p_replaced = False
    for _ in range(30):
        time.sleep(3)
        if spot_oid and not s_done:
            st, sf, _ = _check_order_filled(ex, spot_sym, spot_oid)
            _update_order_fill(symbol, "spot", "sell", spot_oid, spot_coid, sf, st)
            s_exec = sf
            if st == "closed":
                s_done = True
            elif st == "unknown":
                _freeze(symbol, "平仓订单状态未知")
                add_log(f"  ⚠ [{symbol}] 现货平仓订单状态未知，已冻结")
                return False, s_exec, p_exec
            elif st == "canceled" and not s_replaced and spot_qty - sf > 1e-8:
                # 被外部撤销且还有剩余 → 补一次可成交平仓单
                s_replaced = True
                remaining = spot_qty - sf
                px = _cross_price(spot_sym, "sell")
                if px:
                    add_log(f"  [{symbol}] 现货平仓单被撤，补单卖 {remaining:.4f}")
                    spot_oid, spot_coid = _place_limit(ex, spot_sym, "sell", remaining, px,
                                                       intent="close", post_only=False)
                    if not spot_oid:
                        return False, s_exec, p_exec
        if perp_oid and not p_done:
            pt, pf, _ = _check_order_filled(ex, perp_sym, perp_oid)
            _update_order_fill(symbol, "perp", "buy", perp_oid, perp_coid, pf, pt)
            p_exec = pf
            if pt == "closed":
                p_done = True
            elif pt == "unknown":
                _freeze(symbol, "平仓订单状态未知")
                add_log(f"  ⚠ [{symbol}] 合约平仓订单状态未知，已冻结")
                return False, s_exec, p_exec
            elif pt == "canceled" and not p_replaced and perp_qty - pf > 1e-8:
                p_replaced = True
                remaining = perp_qty - pf
                px = _cross_price(perp_sym, "buy")
                if px:
                    add_log(f"  [{symbol}] 合约平仓单被撤，补单买 {remaining:.4f}")
                    perp_oid, perp_coid = _place_limit(ex, perp_sym, "buy", remaining, px,
                                                       intent="close", post_only=False, reduce_only=True)
                    if not perp_oid:
                        return False, s_exec, p_exec
        if s_done and p_done:
            add_log(f"  [{symbol}] 平仓完成 ✓ (现货{s_exec:.4f}/合约{p_exec:.4f})")
            return True, s_exec, p_exec

    # 超时后撤掉剩余
    if spot_oid and not s_done:
        _cancel_order(spot_sym, spot_oid, spot_coid, symbol, "spot", "sell")
    if perp_oid and not p_done:
        _cancel_order(perp_sym, perp_oid, perp_coid, symbol, "perp", "buy")
    add_log(f"  ⚠ [{symbol}] 平仓超时，现货已成交{s_exec:.4f}/合约已成交{p_exec:.4f}")
    return False, s_exec, p_exec


def _normalize_pair_qty(symbol: str, qty: float) -> Optional[float]:
    """★ 统一现货/永续两腿的数量精度（取较粗步长向下取整）。
    两侧市场精度可能不同（如 XRP 现货 0.1 / 永续 0.0001）：不统一时现货被截断
    （9.7833→9.7）而永续不截断（9.7832），造成恒定的 ~0.0832 失衡，触发虚假回滚。
    返回 None 表示小于最小下单量。"""
    import decimal
    coarse = 1e-8
    for s in (_ccxt_spot(symbol), _ccxt_perp(symbol)):
        m = markets.get(s) or {}
        p = float(m.get("precision", {}).get("amount", 1e-8) or 1e-8)
        if p > 0:
            coarse = max(coarse, p)
    q = decimal.Decimal(str(qty))
    step = decimal.Decimal(str(coarse))
    norm = float(q - (q % step))   # 向下取整到步长整数倍
    # 最小下单量校验（取两市场较大的 min）
    mn = 0.0
    for s in (_ccxt_spot(symbol), _ccxt_perp(symbol)):
        m = markets.get(s) or {}
        mn = max(mn, float(m.get("limits", {}).get("amount", {}).get("min", 0) or 0))
    if norm <= 1e-12 or norm + 1e-12 < mn:
        return None
    return norm


def open_position(symbol: str, notional: float, leverage: float,
                  order_size: Optional[float] = None, timeout: Optional[int] = None) -> dict:
    """开仓。★ leverage 参与目标名义计算：target_notional = notional × leverage"""
    _order_size = order_size if order_size else ORDER_SIZE_USDC
    _timeout = timeout if timeout else PAIR_TIMEOUT_S
    spot_sym = _ccxt_spot(symbol)
    perp_sym = _ccxt_perp(symbol)

    if symbol in _frozen:
        return {"ok": False, "error": f"{symbol} 已被冻结（订单状态未知），需人工处理后重启"}
    if _unknown_exposure:
        return {"ok": False, "error": "启动对账发现未知持仓，已禁止开仓，请先核对账本与交易所持仓"}

    # ★ 费率硬门槛（三条件任一不满足 → 拒绝开仓）：
    #   1. 费率为负或零（负费率开仓必然亏损）
    #   2. 年化（按实际结算周期）低于 MIN_NET_APY 门槛
    #   3. 净年化 = 年化 - EST_ROUND_TRIP_COST_APY 成本缓冲 ≤ 0（覆盖不了往返成本）
    rate, apy = _funding_rate_info(perp_sym)
    if rate is None:
        if DRY_RUN:
            add_log(f"[警告] {symbol} 无法获取资金费率（DRY-RUN 放行）")
        else:
            return {"ok": False, "error": "无法获取资金费率，拒绝开仓"}
    if rate <= 0:
        return {"ok": False, "error": f"费率不达标，拒绝开仓: 最新费率 {rate * 100:.4f}%（≤0，负费率开仓必然亏损）"}
    net_apy = apy - EST_ROUND_TRIP_COST_APY
    if apy < MIN_NET_APY:
        return {"ok": False, "error": f"费率不达标，拒绝开仓: 年化 {apy:.1f}% < 门槛 {MIN_NET_APY}%"
                                      f"（最新费率 {rate * 100:.4f}%）"}
    if net_apy <= 0:
        return {"ok": False, "error": f"费率不达标，拒绝开仓: 净年化 {net_apy:.1f}% ≤ 0"
                                      f"（已扣成本缓冲 {EST_ROUND_TRIP_COST_APY}%）"}

    mid = _depth_mid(spot_sym)
    if not mid:
        return {"ok": False, "error": "无参考价"}

    leverage = max(1.0, float(leverage))
    target_notional = notional * leverage   # ★ leverage 真正参与目标名义
    add_log(f"开仓 {symbol}: 本金 {notional:.0f}U × {leverage:.1f}x = 目标名义 {target_notional:.0f}U")

    # ★ 保证金校验（杠杆后口径）：所需保证金 = 目标名义 / 杠杆
    if has_key and not DRY_RUN:
        try:
            col = _get_collateral_data(ex)
            avail = float(col.get("netEquityAvailable", 0) or 0)
            need_margin = target_notional / leverage
            if avail < need_margin:
                return {"ok": False, "error": f"净值可用 {avail:.0f} < 所需保证金 {need_margin:.0f}"}
        except Exception as e:
            add_log(f"[警告] 余额校验失败: {e}")

    # 杠杆上限校验
    max_lev = _get_max_leverage(symbol)
    if max_lev > 0 and leverage > max_lev:
        return {"ok": False, "error": f"杠杆 {leverage}x > 上限 {max_lev}x"}

    pairs = max(1, int(target_notional / _order_size))
    pair_notional = target_notional / pairs
    pair_qty = _normalize_pair_qty(symbol, pair_notional / mid)
    if pair_qty is None:
        return {"ok": False, "error": f"{symbol} 拆单后数量低于最小下单量（精度/步长限制），请加大单笔金额"}
    add_log(f"开仓 {symbol}: 名义 {target_notional:.0f}U {pairs}笔 每笔{pair_notional:.0f}U/{pair_qty:.6f}个"
            f"（已统一两侧精度）")

    ok_pairs = 0
    for i in range(pairs):
        add_log(f"[{symbol}] 第 {i + 1}/{pairs} 对")
        success, spot_filled, perp_filled, msg = execute_pair(symbol, pair_qty, _timeout)
        if success:
            ok_pairs += 1
        if msg:
            add_log(f"  [{symbol}] 说明: {msg}")
        if spot_filled <= 1e-8 and perp_filled <= 1e-8 and not success:
            break  # 首对零成交且失败，不必继续
        time.sleep(2)

    sp = _get_strategy_position(symbol)
    add_log(f"开仓 {symbol} 结束: 成功 {ok_pairs}/{pairs} 对 | 账本 现{sp['spot_qty']:.4f}/合{sp['perp_qty']:.4f}")
    return {"ok": ok_pairs > 0, "pairs_done": ok_pairs, "pairs_total": pairs,
            "strategy_spot": sp["spot_qty"], "strategy_perp": sp["perp_qty"]}


def close_position(symbol: str, order_size: Optional[float] = None) -> dict:
    """★ 平仓：只关闭账本记录的策略持仓，不动人工持仓。
    量取 min(账本量, API 总持有量) 双保险；平仓后验证 USDC 债务归零。"""
    _order_size = order_size if order_size else ORDER_SIZE_USDC

    sp = _get_strategy_position(symbol)
    ledger_spot = sp["spot_qty"]
    ledger_perp = sp["perp_qty"]          # 带符号，空头为负

    spot_qty = ledger_spot
    spot_total = _get_spot_total(symbol)
    if spot_total is not None:
        spot_qty = min(ledger_spot, max(0.0, spot_total))  # 兜底防超卖
    perp_short = abs(ledger_perp) if ledger_perp < 0 else 0.0

    if spot_qty <= 1e-8 and perp_short <= 1e-8:
        return {"ok": False, "error": f"{symbol} 无策略持仓（账本 现货=0 合约=0）"}

    # 合约 mark price（_get_real_position 一并取回）
    _, _, perp_mark = _get_real_position(symbol)
    perp_price = perp_mark if perp_mark > 0 else 1.0

    perp_notional = perp_short * perp_price
    if perp_short > 1e-8:
        pairs = max(1, int(perp_notional / _order_size))
    else:
        # ★ 纯现货平仓一次卖完（原公式 spot_qty×100/order_size 会把 125 个拆成 125 笔碎单）
        pairs = 1
    pair_spot = spot_qty / pairs
    pair_perp = perp_short / pairs

    add_log(f"平仓 {symbol}: 账本 现货{spot_qty:.4f} 合约{perp_short:.4f} → {pairs}笔")
    total_spot_closed = 0.0
    total_perp_closed = 0.0
    ok_pairs = 0
    for i in range(pairs):
        success, s_filled, p_filled = close_pair(symbol, pair_spot, pair_perp)
        total_spot_closed += s_filled
        total_perp_closed += p_filled
        if success:
            ok_pairs += 1
        time.sleep(2)

    sp2 = _get_strategy_position(symbol)
    # ★ USDC 债务归零验证（卖出单已带 autoBorrowRepay，卖出所得自动还款）
    debt = _get_usdc_borrow()
    if debt is None:
        debt_msg = "无法确认 USDC 债务状态，请人工核对"
        debt_ok = True
    elif debt > 1e-8:
        debt_msg = f"USDC 债务未归零: {debt:.6f}，请人工检查"
        debt_ok = False
    else:
        debt_msg = "USDC 债务已归零"
        debt_ok = True

    add_log(f"平仓 {symbol} 结束: 成功 {ok_pairs}/{pairs} 对 | 账本剩余 现{sp2['spot_qty']:.4f}/"
            f"合{sp2['perp_qty']:.4f} | {debt_msg}")
    # ★ 债务未归零时平仓不算完全成功（审计要求：只有债务低于最小精度才认定策略关闭）
    return {"ok": ok_pairs > 0 and debt_ok, "pairs_done": ok_pairs, "pairs_total": pairs,
            "spot_remaining": sp2["spot_qty"], "perp_remaining": sp2["perp_qty"],
            "debt": debt_msg}


def _adjust_ledger(symbol: str, leg: str, qty: float):
    """对账时把账本某腿直接校正为给定值（仅用于微小尘差自动对齐）"""
    conn = _db_conn()
    try:
        pos = conn.execute("SELECT spot_qty, perp_qty FROM strategy_positions WHERE symbol=?",
                           (symbol,)).fetchone()
        s = float(pos["spot_qty"]) if pos else 0.0
        p = float(pos["perp_qty"]) if pos else 0.0
        if leg == "spot":
            s = qty
        else:
            p = qty
        conn.execute(
            "INSERT INTO strategy_positions (symbol, spot_qty, perp_qty, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET spot_qty=excluded.spot_qty, "
            "perp_qty=excluded.perp_qty, updated_at=excluded.updated_at",
            (symbol, s, p, datetime.now().isoformat()))
        conn.commit()
    finally:
        conn.close()


def _market_step(symbol: str, leg: str) -> float:
    """市场最小数量步长（容差基准）。spot 用现货市场精度，perp 用永续市场精度。"""
    s = _ccxt_spot(symbol) if leg == "spot" else _ccxt_perp(symbol)
    m = markets.get(s) or {}
    return float(m.get("precision", {}).get("amount", 1e-8) or 1e-8)


def _reconcile_positions():
    """★ 启动对账：真实持仓 vs 账本（真实持仓 ∪ 账本持仓 并集双向比对）。
    - 差异 ≤ 市场最小步长（手续费扣币/精度截断产生的尘差）→ 自动对齐账本，不影响交易
    - 差异 > 步长（真实未知敞口/幽灵持仓）→ _unknown_exposure=True，禁止开新仓"""
    global _unknown_exposure
    if not has_key or DRY_RUN:
        add_log("[对账] DRY-RUN/无 key，跳过")
        return
    perp_tradable = {s.split("/")[0] for s in perp_symbols}
    issues = 0

    # 收集真实持仓: {sym: {"spot": qty, "perp": qty(signed)}}
    real: Dict[str, dict] = {}
    try:
        ps = ex.fetch_positions() or []
        for p in ps:
            base = p["symbol"].split("/")[0]
            if base not in perp_tradable:
                continue
            contracts = float(p.get("contracts", 0) or 0)
            cs = _contract_size(p["symbol"])
            q = contracts * cs
            side = p.get("side")
            if side == "short" and q > 0:
                q = -q
            elif side == "long" and q < 0:
                q = -q
            real.setdefault(base, {"spot": 0.0, "perp": 0.0})["perp"] = q
    except Exception as e:
        issues += 1
        add_log(f"[对账] 无法读取合约持仓: {e}")

    try:
        col = _get_collateral_data(ex)
        for ci in col.get("collateral", []) or []:
            sym = ci.get("symbol", "")
            if sym not in perp_tradable:
                continue
            real.setdefault(sym, {"spot": 0.0, "perp": 0.0})["spot"] = float(ci.get("totalQuantity", 0) or 0)
    except Exception as e:
        issues += 1
        add_log(f"[对账] 无法读取现货持仓: {e}")

    # 账本持仓
    ledger = {r["symbol"]: {"spot": r["spot_qty"], "perp": r["perp_qty"]}
              for r in _all_strategy_positions()}

    # ★ 并集双向比对（含微差自动对齐）
    for sym in set(real) | set(ledger):
        rq = real.get(sym, {"spot": 0.0, "perp": 0.0})
        lq = ledger.get(sym, {"spot": 0.0, "perp": 0.0})
        for leg, key in (("现货", "spot"), ("合约", "perp")):
            r, l = rq[key], lq[key]
            tol = max(1e-6, _market_step(sym, key))
            if abs(r - l) <= tol:
                if abs(r - l) > 1e-10:
                    _adjust_ledger(sym, key, r)
                    add_log(f"[对账] {sym} {leg}: 账本 {l:.6f} → 真实 {r:.6f}（手续费/截断尘差，已自动对齐）")
                continue
            issues += 1
            add_log(f"[对账] ⚠ {sym} {leg}: 真实 {r:.6f} vs 账本 {l:.6f} → 未知敞口")

    if issues:
        _unknown_exposure = True
        add_log(f"[对账] 发现 {issues} 处未知敞口，已禁止开仓（请人工核对：交易所真实持仓 vs 账本，"
                f"必要时清理 arb_ledger.db 中对应的幽灵持仓）")
    else:
        add_log("[对账] 账本与真实持仓一致 ✓")


# =====================================================================
# 状态更新 — /api/state 数据源
# =====================================================================

def _build_state() -> dict:
    """构建完整的 state 字典（供 /api/state 和缓存使用）"""
    positions = {}
    perp_positions = []
    collateral_data: dict = {}
    collateral_items: list = []
    balances = []
    total_assets_value = None
    maintenance_margin = None

    # 持仓
    if has_key:
        try:
            perp_positions = ex.fetch_positions() or []
        except Exception:
            perp_positions = []

    # 抵押品（含借贷）
    if has_key:
        try:
            collateral_data = _get_collateral_data(ex) or {}
            collateral_items = collateral_data.get("collateral", []) or []
            total_assets_value = float(collateral_data.get("assetsValue", 0) or 0)
            # marginFraction 是账户级实际维持保证金率（已经是百分比数值，如 2.043 表示 2.0%）
            mf = float(collateral_data.get("marginFraction", 0) or 0)
            maintenance_margin = round(mf, 1) if mf > 0 else None
        except Exception:
            collateral_data = {}
            collateral_items = []

    # 构建持仓视图（★ perp_qty 带符号 + 方向字段）
    for pp in perp_positions:
        sym = pp["symbol"].split("/")[0]
        if not sym:
            continue
        contracts = float(pp.get("contracts", 0) or 0)
        cs = _contract_size(pp["symbol"])
        perp_qty = contracts * cs
        perp_side = pp.get("side") or ("long" if perp_qty >= 0 else "short")
        if perp_side == "short" and perp_qty > 0:
            perp_qty = -perp_qty
        perp_notional = abs(float(pp.get("notional", 0) or 0))
        perp_entry = float(pp.get("entryPrice", 0) or 0)
        perp_mark = float(pp.get("markPrice", 0) or 1)
        perp_pnl_unrealized = round(float(pp.get("unrealizedPnl", 0) or 0), 4)

        # 从现货抵押品中找匹配
        spot_qty = 0.0
        spot_mark = 0.0
        for ci in collateral_items:
            if ci.get("symbol") == sym:
                spot_qty = float(ci.get("totalQuantity", 0) or 0)
                spot_mark = float(ci.get("assetMarkPrice", 0) or 0)
                break

        # 资金费率（带缓存 TTL 60s）
        funding_rate = None
        cache_key = f"{sym}/USDC:USDC"
        now_ts = time.time()
        with _cache_lock:
            cached = _funding_rate_cache.get(cache_key)
            if cached and now_ts - cached[1] < 60:
                funding_rate = cached[0]
        if funding_rate is None:
            _, apy = _funding_rate_info(cache_key)
            if apy is not None:
                funding_rate = round(apy, 1)
                with _cache_lock:
                    _funding_rate_cache[cache_key] = (funding_rate, now_ts)

        positions[sym] = {
            "symbol": sym,
            "spot_qty": spot_qty,
            "spot_entry": None,
            "spot_price": spot_mark,
            "spot_value": round(spot_qty * spot_mark, 2) if spot_mark else None,
            "perp_qty": perp_qty,
            "perp_side": perp_side,
            "perp_entry": perp_entry,
            "perp_mark": perp_mark,
            "perp_notional": perp_notional,
            "perp_pnl_funding": 0,
            "perp_pnl_unrealized": perp_pnl_unrealized,
            "perp_pnl_realized": 0,
            "funding_rate": funding_rate,
        }

    # 扫描裸现货（有现货持仓但无合约持仓的币种，前端持仓表不可见 → 补上）
    perp_tradable = {s.split("/")[0] for s in perp_symbols}
    has_perp_syms = {pp["symbol"].split("/")[0] for pp in perp_positions}
    for ci in collateral_items:
        sym = ci.get("symbol", "")
        spot_qty = float(ci.get("totalQuantity", 0) or 0)
        if spot_qty <= 1e-8 or sym in positions or sym in has_perp_syms:
            continue
        if sym not in perp_tradable:
            continue
        spot_mark = float(ci.get("assetMarkPrice", 0) or 0)
        positions[sym] = {
            "symbol": sym,
            "spot_qty": spot_qty,
            "spot_entry": None,
            "spot_price": spot_mark,
            "spot_value": round(spot_qty * spot_mark, 2) if spot_mark else None,
            "perp_qty": 0,
            "perp_side": None,
            "perp_entry": 0,
            "perp_mark": 0,
            "perp_notional": 0,
            "perp_pnl_funding": 0,
            "perp_pnl_unrealized": 0,
            "perp_pnl_realized": 0,
            "funding_rate": None,
        }

    # 余额列表
    for ci in collateral_items:
        balances.append({
            "asset": ci.get("symbol", "?"),
            "available": float(ci.get("availableQuantity", 0) or 0),
            "locked": float(ci.get("openOrderQuantity", 0) or 0),
            "lend": float(ci.get("lendQuantity", 0) or 0),
            "total_notional": float(ci.get("balanceNotional", 0) or 0),
        })

    # 活跃订单
    active_orders.clear()
    if has_key:
        try:
            raw = ex.fetch_open_orders() or []
            for o in raw:
                sym = o.get("symbol", "")
                if not sym:
                    continue
                base = sym.split("/")[0]
                o["market"] = "合约" if ":USDC" in sym else "现货"
                o["sym"] = sym
                o["qty"] = float(o.get("amount", 0) or 0)
                o["executedQty"] = float(o.get("filled", 0) or 0)
                o["price"] = float(o.get("price", 0) or 0)
                o["side"] = o.get("side", "?")
                if base not in active_orders:
                    active_orders[base] = []
                active_orders[base].append(o)
        except Exception:
            pass

    # 日志：从 bpx_arb.log 文件尾读（重启不丢），格式转换后取最近 30 条
    logs = operation_log[-30:]  # 兜底
    try:
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bpx_arb.log")
        if os.path.exists(log_path):
            with open(log_path, encoding="utf-8") as f:
                lines = f.readlines()
            # bpx_arb.log 格式: "2026-08-06 14:49:38,740 INFO some message"
            # 转成前端格式: "[14:49:38] some message"（过滤 Flask HTTP 请求日志行）
            import re
            parsed = []
            for line in lines:
                m = re.match(r"\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2}),\d+ INFO (.+)", line)
                if m:
                    msg = m.group(2)
                    # 跳过 Flask 请求日志（"127.0.0.1 - - ..."）、启动标语
                    if " - - [" in msg or '"GET' in msg or '"POST' in msg:
                        continue
                    parsed.append(f"[{m.group(1)}] {msg}")
            logs = parsed[-30:] if parsed else operation_log[-30:]
    except Exception:
        pass

    return {
        "version": VERSION,
        "dry_run": DRY_RUN,
        "has_key": has_key,
        "positions": positions,
        "active_orders": dict(active_orders),
        "balances": balances,
        "maintenance_margin_ratio": maintenance_margin,
        "total_assets_value": total_assets_value,
        "strategy_ledger": _all_strategy_positions(),
        "unknown_exposure": _unknown_exposure,
        "frozen": sorted(_frozen),
        "exposed": sorted(_exposed),
        "logs": logs,
    }


# =====================================================================
# Flask API（★ v5.1: 登录保护 + 任务模式 + 127.0.0.1）
# =====================================================================

_login_fails: Dict[str, list] = {}   # {ip: [fail_ts, ...]} 登录防爆破


def _is_authed() -> bool:
    return bool(session.get("authed"))


@app.before_request
def _auth_guard():
    """★ 全局登录保护：除登录页与登录接口外，全部要求已登录。
    旧方案的 X-Auth-Token 注入页面=打开源码就能拿到，公网形同虚设；
    v5.1 改为 Flask session + HttpOnly cookie，密码不经过前端源码。"""
    if request.path in ("/login", "/api/login"):
        return None
    if _is_authed():
        return None
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "未登录"}), 401
    return redirect("/login")


@app.route("/login")
def login_page():
    if _is_authed():
        return redirect("/")
    return render_template("login.html", version=VERSION)


@app.route("/api/login", methods=["POST"])
def api_login():
    """登录：校验 BPX_WEB_USER/BPX_WEB_PASSWORD，成功写入 session"""
    ip = request.remote_addr or "?"
    fails = [t for t in _login_fails.get(ip, []) if time.time() - t < 60]
    if len(fails) >= 5:
        return jsonify({"ok": False, "error": "尝试次数过多，请 60 秒后再试"}), 429
    data = request.get_json(silent=True) or {}
    user = str(data.get("username", ""))
    password = str(data.get("password", ""))
    if (hmac.compare_digest(user, BPX_WEB_USER)
            and hmac.compare_digest(password, BPX_WEB_PASSWORD)):
        session["authed"] = True
        session.permanent = False
        add_log(f"网页登录成功: {user}")
        return jsonify({"ok": True})
    _login_fails.setdefault(ip, []).append(time.time())
    return jsonify({"ok": False, "error": "用户名或密码错误"}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


def _safe_error(e: Exception) -> str:
    """★ 错误脱敏：不向前端返回原始交易所错误信息"""
    return "交易接口异常，详见服务端日志"


@app.route("/")
def index():
    return render_template("bpx_arb.html", version=VERSION)


@app.route("/api/symbols")
def api_symbols():
    """可选币种列表"""
    try:
        col = ex.publicGetApiV1Collateral()
        perp_set = {s.split("/")[0] for s in perp_symbols}
        result = []
        for c in col:
            sym = c.get("symbol", "")
            if sym not in perp_set:
                continue
            imf = float(c.get("imfFunction", {}).get("base", 1))
            max_lev = math.floor(1 / imf) if 0 < imf < 100 else 0
            kind = c.get("haircutFunction", {}).get("kind", {})
            hc = float(kind.get("base", kind.get("weight", 0)))

            latest_rate = None
            latest_apy = None
            cache_key = f"{sym}/USDC:USDC"
            now_ts = time.time()
            with _cache_lock:
                cached = _funding_rate_cache.get(cache_key)
                if cached and now_ts - cached[1] < 60:
                    latest_apy = cached[0]
            if latest_apy is None:
                rate, apy = _funding_rate_info(cache_key)
                if rate is not None and apy is not None:
                    latest_rate = rate
                    latest_apy = round(apy, 1)
                    with _cache_lock:
                        _funding_rate_cache[cache_key] = (latest_apy, now_ts)
            result.append({
                "symbol": sym,
                "max_leverage": max_lev,
                "haircut": hc,
                "latest_rate": latest_rate,
                "latest_apy": round(latest_apy, 1) if latest_apy else None,
            })
        return jsonify(result)
    except Exception:
        return jsonify({"error": _safe_error(Exception("symbols"))}), 500


@app.route("/api/state")
def api_state():
    """返回当前状态"""
    return jsonify(_build_state())


@app.route("/api/task/<task_id>")
def api_task(task_id):
    """查询后台任务结果（开仓/平仓的真实执行结果不再丢失）"""
    t = _task_results.get(task_id)
    if not t:
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    return jsonify({"task_id": task_id, "status": t["status"], "result": t["result"]})


def _task_submit(task_id: str, symbol: str, fn):
    """启动后台任务并登记 _inflight"""
    _task_results[task_id] = {"status": "running", "result": None}
    with _trade_lock:
        _inflight.add(symbol)

    def _run():
        try:
            res = fn()
            _task_results[task_id] = {"status": "done", "result": res}
        except Exception as e:
            add_log(f"[任务失败] {task_id}: {type(e).__name__} {str(e)[:120]}")
            _task_results[task_id] = {"status": "error",
                                      "result": {"ok": False, "error": _safe_error(e)}}
        finally:
            with _trade_lock:
                _inflight.discard(symbol)

    threading.Thread(target=_run, daemon=True).start()


@app.route("/api/open", methods=["POST"])
def api_open():
    """开仓（任务模式：返回 task_id，结果由 /api/task 查询）"""
    data = request.get_json(silent=True) or {}
    symbol = str(data.get("symbol", "")).strip()
    try:
        notional = float(data.get("notional", 0) or 0)
        leverage = float(data.get("leverage", 1) or 1)
        order_size = float(data.get("order_size", 0) or 0)
        timeout = int(data.get("timeout", 0) or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "参数无效"}), 400

    if not symbol or notional <= 0:
        return jsonify({"ok": False, "error": "币种或金额无效"}), 400
    if symbol in _frozen:
        return jsonify({"ok": False, "error": f"{symbol} 已被冻结，需人工处理"}), 409
    if _unknown_exposure:
        return jsonify({"ok": False, "error": "启动对账发现未知持仓，已禁止开仓"}), 409
    with _trade_lock:
        if symbol in _inflight:
            return jsonify({"ok": False, "error": f"{symbol} 有操作进行中"}), 409

    task_id = f"open-{int(time.time() * 1000)}"
    add_log(f"[提交] 开仓 {symbol} {notional:.0f}U×{leverage:.1f}x → task {task_id}")
    _task_submit(task_id, symbol, lambda: open_position(symbol, notional, leverage,
                                                        order_size or None, timeout or None))
    return jsonify({"ok": True, "task_id": task_id, "msg": "已提交后台执行"})


@app.route("/api/close", methods=["POST"])
def api_close():
    """平仓（任务模式：返回 task_id）"""
    data = request.get_json(silent=True) or {}
    symbol = str(data.get("symbol", "")).strip()
    try:
        order_size = float(data.get("order_size", 0) or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "参数无效"}), 400
    if not symbol:
        return jsonify({"ok": False, "error": "币种无效"}), 400
    with _trade_lock:
        if symbol in _inflight:
            return jsonify({"ok": False, "error": f"{symbol} 有操作进行中"}), 409

    task_id = f"close-{int(time.time() * 1000)}"
    add_log(f"[提交] 平仓 {symbol} → task {task_id}")
    _task_submit(task_id, symbol, lambda: close_position(symbol, order_size or None))
    return jsonify({"ok": True, "task_id": task_id, "msg": "已提交后台执行"})


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    """撤单"""
    data = request.get_json(silent=True) or {}
    symbol = str(data.get("symbol", "")).strip()
    if not symbol or not has_key:
        return jsonify({"ok": False, "error": "无目标或无 key"}), 400

    try:
        spot_sym = _ccxt_spot(symbol)
        perp_sym = _ccxt_perp(symbol)
        resp_spot = ex.cancel_all_orders(spot_sym)
        resp_perp = ex.cancel_all_orders(perp_sym)
        spot_cnt = len(resp_spot) if isinstance(resp_spot, list) else 0
        perp_cnt = len(resp_perp) if isinstance(resp_perp, list) else 0
        add_log(f"手动撤单 {symbol}: 现货{spot_cnt}笔 合约{perp_cnt}笔")
        return jsonify({"ok": True})
    except Exception:
        return jsonify({"ok": False, "error": _safe_error(Exception("cancel"))})


# =====================================================================
# 启动
# =====================================================================

if __name__ == "__main__":
    _init_db()
    _reconcile_positions()
    logger.info("======== 启动 Backpack 套利面板 v%s (ccxt) 端口 %d ========", VERSION, PORT)
    logger.info("模式: %s | 单笔: %dU | 超时: %ds | 净年化门槛: %.1f%% | 滑点上限: %dbps",
                "DRY-RUN" if DRY_RUN else "实盘", ORDER_SIZE_USDC, PAIR_TIMEOUT_S,
                MIN_NET_APY, MAX_SLIPPAGE_BPS)
    app.run(host=HOST, port=PORT, threaded=True, debug=False)
