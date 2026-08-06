# -*- coding: utf-8 -*-
"""
Backpack 资金费率套利交易脚本 v4.0 — ccxt 重写版 (2026-08-05)
Flask 后端 + 前端操作面板

端口 5055 | 实盘需 BPX_LIVE=1 环境变量
底层：ccxt 4.5.71（官方 Backpack 支持）

策略：
  现货腿永远 maker（买一价）| 合约腿初始 maker/post_only（卖一价）
  一条腿成交后另一条改 taker（对手方最优价）
  两腿都不动满 3 分钟 → 撤单重挂

v4.0 改动：
  ★ 底层从 bpx-py SDK 迁移到 ccxt 4.x
  ★ 精度交给 ccxt（amount_to_precision / price_to_precision，TICK_SIZE 模式）
  ★ bids 排序 ccxt 自动归一为降序（bids[0]=买一）
  ★ 错误处理 ccxt 抛类型化异常（InvalidOrder / InsufficientFunds 等）
  ★ 抵押品数据用 implicit API privateGetApiV1CapitalCollateral()
  ★ 后端输出结构与前版 HTML 完全对齐
"""
import json
import logging
import math
import os
import sys
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import ccxt
from flask import Flask, jsonify, render_template, request

# =====================================================================
# 配置 — 沿用现有策略参数
# =====================================================================
PORT = 5055

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
PAIR_TIMEOUT_S = 180               # 单对超时秒数（3分钟）
MAX_RETRIES = 3                    # 最大重挂次数
MIN_WEEK_APY = 10.0                # 费率警告阈值
BPX_PUBLIC_KEY = os.environ.get("BPX_PUBLIC_KEY", "")
BPX_SECRET_KEY = os.environ.get("BPX_SECRET_KEY", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("bpx_arb.log", encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger("arb")

app = Flask(__name__)

# =====================================================================
# 初始化 ccxt
# =====================================================================
has_key = bool(BPX_PUBLIC_KEY and BPX_SECRET_KEY)
ex: ccxt.backpack = ccxt.backpack({
    "apiKey": BPX_PUBLIC_KEY,
    "secret": BPX_SECRET_KEY,
    "enableRateLimit": True,
})
logger.info("ccxt %s 已初始化 | %s", ccxt.__version__, "DRY-RUN" if DRY_RUN else "实盘")
if not has_key:
    logger.warning("未配置 API key，仅行情可用")

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
position_state: Dict[str, Any] = {}          # {symbol: {spot_qty, perp_qty, ...}}
active_orders: Dict[str, List[dict]] = {}    # {base_coin: [order_info, ...]}
operation_log: List[str] = []
_trade_lock = threading.Lock()
_inflight: set = set()
_last_state: Dict[str, Any] = {}
_state_lock = threading.Lock()
_cache_lock = threading.Lock()
_funding_rate_cache: Dict[str, tuple] = {}  # {sym: (apy, timestamp)} TTL 60s


def add_log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    operation_log.append(entry)
    if len(operation_log) > 200:
        operation_log.pop(0)
    logger.info(msg)


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


# =====================================================================
# 下单 / 撤单 / 查单
# =====================================================================

def _place_limit(exchange, ccxt_sym: str, side: str, amount: float,
                 price: float, post_only: bool = True) -> Optional[str]:
    """下限价单。精度由 ccxt 的 amount_to_precision / price_to_precision 自动处理"""
    action = f"{'maker' if post_only else 'taker'} {side} {ccxt_sym} {amount:.4f} @ {price}"
    if DRY_RUN or not has_key:
        add_log(f"[DRY] {action}")
        return f"dry-{int(time.time() * 1000)}"

    try:
        params = {}
        if post_only:
            params["postOnly"] = True

        # 如果是现货，加 autoLend/autoBorrow/autoLendRedeem
        # autoLendRedeem: 平仓卖现货时，若持仓在 lend 状态自动赎回再卖（否则 avail=0 会被拒）
        if "/USDC" in ccxt_sym and ":USDC" not in ccxt_sym:
            params["autoLend"] = True
            params["autoLendRedeem"] = True
            params["autoBorrow"] = True

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
            add_log(f"{action} → order_id={oid}")
            return str(oid)
        add_log(f"[FAIL] {action}: order={order}")
    except ccxt.InvalidOrder as e:
        add_log(f"[FAIL] InvalidOrder {action}: {str(e)[:120]}")
    except ccxt.InsufficientFunds as e:
        add_log(f"[FAIL] 余额不足 {action}: {str(e)[:120]}")
    except ccxt.NetworkError as e:
        add_log(f"[FAIL] 网络异常 {action}: {e}")
    except Exception as e:
        add_log(f"[FAIL] {action}: {type(e).__name__} {str(e)[:120]}")
    return None


def _cancel_order(exchange, ccxt_sym: str, order_id: str) -> bool:
    if order_id.startswith("dry-"):
        add_log(f"[DRY] 撤单 {ccxt_sym} {order_id}")
        return True
    if DRY_RUN or not has_key:
        return True
    try:
        exchange.cancel_order(order_id, ccxt_sym)
        add_log(f"撤单 {ccxt_sym} {order_id}")
        return True
    except ccxt.OrderNotFound:
        add_log(f"撤单 {ccxt_sym} {order_id}: 订单已不存在（可能已成交）")
        return True
    except Exception as e:
        add_log(f"[FAIL] 撤单 {ccxt_sym} {order_id}: {type(e).__name__} {str(e)[:120]}")
        return False


def _check_order_filled(exchange, ccxt_sym: str, order_id: str) -> tuple:
    """返回 (是否完全成交, 已成交量)。ccxt 会自动抛异常处理 4xx"""
    if order_id.startswith("dry-"):
        return True, ORDER_SIZE_USDC / 100
    if DRY_RUN or not has_key:
        return True, 1.0
    try:
        order = exchange.fetch_order(order_id, ccxt_sym)
        status = order.get("status", "")
        filled = float(order.get("filled", 0))
        amount = float(order.get("amount", 0))
        if status == "closed":
            return True, filled
        if status == "canceled":
            return False, filled
        return (filled >= amount and amount > 0, filled)
    except ccxt.OrderNotFound:
        # 查不到 = 可能已成交 + 已从挂单列表移除
        return True, 1.0
    except Exception:
        return False, 0


# =====================================================================
# 交易逻辑 — 沿用原有策略
# =====================================================================

def execute_pair(symbol: str, qty: float, timeout_s: int = PAIR_TIMEOUT_S):
    """执行一对：现货买 + 合约卖。返回 (success, spot_total_filled, perp_total_filled)
    即使部分成交也如实报告，不再丢弃已成交的腿。"""
    spot_sym = _ccxt_spot(symbol)
    perp_sym = _ccxt_perp(symbol)

    spot_bid, spot_ask = _depth_bbo(spot_sym)
    perp_bid, perp_ask = _depth_bbo(perp_sym)
    if not spot_bid or not spot_ask or not perp_bid or not perp_ask:
        add_log(f"[跳] {symbol} 无盘口数据")
        return False, 0.0, 0.0

    spot_total = 0.0
    perp_total = 0.0

    for attempt in range(1, MAX_RETRIES + 1):
        spot_amount = qty - spot_total
        perp_amount = qty - perp_total
        add_log(f"  [{symbol}] 第{attempt}次: 现货买@{spot_bid:.6f}({spot_amount:.4f}个) 合约卖@{perp_ask:.6f}({perp_amount:.4f}个)")

        # 只对有剩余的腿下单
        spot_oid = None
        perp_oid = None
        if spot_amount > 1e-8:
            spot_oid = _place_limit(ex, spot_sym, "buy", spot_amount, spot_bid, post_only=True)
        if perp_amount > 1e-8:
            perp_oid = _place_limit(ex, perp_sym, "sell", perp_amount, perp_ask, post_only=True)

        if (spot_amount > 1e-8 and not spot_oid) or (perp_amount > 1e-8 and not perp_oid):
            if spot_oid:
                _cancel_order(ex, spot_sym, spot_oid)
            if perp_oid:
                _cancel_order(ex, perp_sym, perp_oid)
            return False, spot_total, perp_total
        if not spot_oid and not perp_oid:
            return True, spot_total, perp_total  # 两腿在上轮全已成交

        start = time.time()
        s_exec = 0.0
        p_exec = 0.0
        while time.time() - start < timeout_s:
            time.sleep(2)

            s_ok = not spot_oid  # 无订单视为已成
            p_ok = not perp_oid
            s_exec = 0.0
            p_exec = 0.0
            if spot_oid:
                s_ok, s_exec = _check_order_filled(ex, spot_sym, spot_oid)
            if perp_oid:
                p_ok, p_exec = _check_order_filled(ex, perp_sym, perp_oid)

            if s_ok and p_ok:
                spot_total += spot_amount
                perp_total += perp_amount
                add_log(f"  [{symbol}] 两腿均成交 ✓ (累计 现{spot_total:.4f}/合{perp_total:.4f})")
                return True, spot_total, perp_total

            if s_ok and spot_amount > 1e-8:
                # 现货腿全成，合约需追
                spot_total += spot_amount
                spot_amount = 0.0
                remaining = max(0.0001, perp_amount - p_exec) if p_exec > 0 else perp_amount
                add_log(f"  [{symbol}] 现货成交，合约改 taker (剩余{remaining:.4f})")
                _cancel_order(ex, perp_sym, perp_oid)
                _, new_perp_ask = _depth_bbo(perp_sym)
                if new_perp_ask:
                    perp_oid = _place_limit(ex, perp_sym, "sell", remaining, new_perp_ask, post_only=False)
                    perp_amount = remaining
                    if not perp_oid:
                        perp_total += p_exec
                        add_log(f"  ⚠ [{symbol}] 现货已成交但合约无法成交！裸多预警！(已累计 现{spot_total:.4f}/合{perp_total:.4f})")
                        return False, spot_total, perp_total
                else:
                    perp_total += p_exec
                    add_log(f"  ⚠ [{symbol}] 合约 taker 无盘口！裸多预警！")
                    return False, spot_total, perp_total

            elif p_ok and perp_amount > 1e-8:
                perp_total += perp_amount
                perp_amount = 0.0
                remaining = max(0.0001, spot_amount - s_exec) if s_exec > 0 else spot_amount
                add_log(f"  [{symbol}] 合约成交，现货改 taker (剩余{remaining:.4f})")
                _cancel_order(ex, spot_sym, spot_oid)
                new_spot_bid, _ = _depth_bbo(spot_sym)
                if new_spot_bid:
                    spot_oid = _place_limit(ex, spot_sym, "buy", remaining, new_spot_bid, post_only=False)
                    spot_amount = remaining
                    if not spot_oid:
                        spot_total += s_exec
                        add_log(f"  ⚠ [{symbol}] 合约已成交但现货无法成交！裸空预警！(已累计 现{spot_total:.4f}/合{perp_total:.4f})")
                        return False, spot_total, perp_total
                else:
                    spot_total += s_exec
                    add_log(f"  ⚠ [{symbol}] 现货 taker 无盘口！裸空预警！")
                    return False, spot_total, perp_total

        # 超时，记录本次成交，撤单重试
        if spot_amount > 1e-8:
            spot_total += s_exec
            _cancel_order(ex, spot_sym, spot_oid)
        if perp_amount > 1e-8:
            perp_total += p_exec
            _cancel_order(ex, perp_sym, perp_oid)
        add_log(f"  [{symbol}] 超时，撤单重挂 (已累计 现{spot_total:.4f}/合{perp_total:.4f})")
        time.sleep(2)
        spot_bid, spot_ask = _depth_bbo(spot_sym)
        perp_bid, perp_ask = _depth_bbo(perp_sym)
        if not spot_bid or not spot_ask or not perp_bid or not perp_ask:
            return False, spot_total, perp_total

    add_log(f"  [{symbol}] {MAX_RETRIES}次重试均失败 (累计 现{spot_total:.4f}/合{perp_total:.4f})")
    return False, spot_total, perp_total


def _get_real_position(symbol: str):
    """从 API 读取真实持仓（现货 + 合约 + mark price），不依赖 position_state 记账。
    返回 (spot_qty, perp_qty, perp_mark_price)。"""
    spot_qty = 0.0
    perp_qty = 0.0
    perp_mark = 0.0
    if has_key and not DRY_RUN:
        # 合约量 + mark price（一次 fetch_positions 拿齐）
        try:
            ps = ex.fetch_positions() or []
            for p in ps:
                if p["symbol"].split("/")[0] == symbol:
                    perp_qty = abs(float(p.get("contracts", 0)))
                    perp_mark = float(p.get("markPrice", 0) or p.get("entryPrice", 0))
                    break
        except Exception:
            pass
        # 现货量（collateral 的 totalQuantity）
        try:
            col = _get_collateral_data(ex)
            for ci in col.get("collateral", []) or []:
                if ci.get("symbol") == symbol:
                    spot_qty = float(ci.get("totalQuantity", 0))
                    break
        except Exception:
            pass
    return round(spot_qty, 8), round(perp_qty, 8), round(perp_mark, 8)


def close_pair(symbol: str, spot_qty: float, perp_qty: float) -> tuple:
    """平仓一对：现货卖 + 合约买入平仓。两腿各自独立 taker 直出，量可不等。
    返回 (是否完全成交, spot_filled, perp_filled)"""
    spot_sym = _ccxt_spot(symbol)
    perp_sym = _ccxt_perp(symbol)

    has_spot = spot_qty > 1e-8
    has_perp = perp_qty > 1e-8

    spot_oid, perp_oid = None, None
    if has_spot:
        _, spot_ask = _depth_bbo(spot_sym)
        if spot_ask:
            spot_oid = _place_limit(ex, spot_sym, "sell", spot_qty, spot_ask, post_only=False)
    if has_perp:
        perp_bid, _ = _depth_bbo(perp_sym)
        if perp_bid:
            perp_oid = _place_limit(ex, perp_sym, "buy", perp_qty, perp_bid, post_only=False)

    if has_spot and not spot_oid:
        if perp_oid:
            _cancel_order(ex, perp_sym, perp_oid)
        add_log(f"  [{symbol}] 现货平仓下单失败")
        return False, 0.0, 0.0
    if has_perp and not perp_oid:
        if spot_oid:
            _cancel_order(ex, spot_sym, spot_oid)
        add_log(f"  [{symbol}] 合约平仓下单失败")
        return False, 0.0, 0.0

    # 如果没有需要平仓的腿（理论上调用方会先检查，这里兜底）
    if not spot_oid and not perp_oid:
        return True, 0.0, 0.0

    # 等待各腿成交，互相独立
    s_done, s_executed = not has_spot, spot_qty if not has_spot else 0.0
    p_done, p_executed = not has_perp, perp_qty if not has_perp else 0.0
    for _ in range(30):
        time.sleep(3)
        if spot_oid and not s_done:
            s_ok, s_filled = _check_order_filled(ex, spot_sym, spot_oid)
            s_executed = s_filled
            if s_ok:
                s_done = True
        if perp_oid and not p_done:
            p_ok, p_filled = _check_order_filled(ex, perp_sym, perp_oid)
            p_executed = p_filled
            if p_ok:
                p_done = True
        if s_done and p_done:
            add_log(f"  [{symbol}] 平仓完成 ✓ (现货{s_executed:.4f}/合约{p_executed:.4f})")
            return True, s_executed, p_executed
        if s_done and has_perp:
            add_log(f"  [{symbol}] 现货已卖出，等待合约平仓...")
        if p_done and has_spot:
            add_log(f"  [{symbol}] 合约已平仓，等待现货卖出...")

    # 超时后撤掉剩余
    if spot_oid and not s_done:
        _cancel_order(ex, spot_sym, spot_oid)
    if perp_oid and not p_done:
        _cancel_order(ex, perp_sym, perp_oid)
    add_log(f"  ⚠ [{symbol}] 平仓超时，现货已成交{s_executed:.4f}/合约已成交{p_executed:.4f}")
    return False, s_executed, p_executed


def open_position(symbol: str, notional: float, leverage: float, order_size=None, timeout=None) -> dict:
    """开仓"""
    _order_size = order_size if order_size else ORDER_SIZE_USDC
    _timeout = timeout if timeout else PAIR_TIMEOUT_S
    spot_sym = _ccxt_spot(symbol)

    mid = _depth_mid(spot_sym)
    if not mid:
        return {"ok": False, "error": "无参考价"}

    # 余额校验（用 netEquityAvailable）
    if has_key and not DRY_RUN:
        try:
            col = _get_collateral_data(ex)
            avail = float(col.get("netEquityAvailable", 0))
            if avail < notional:
                return {"ok": False, "error": f"净值可用 {avail:.0f} < 开仓 {notional:.0f}"}
        except Exception:
            pass

    # 杠杆校验
    max_lev = _get_max_leverage(symbol)
    if max_lev > 0 and leverage > max_lev:
        return {"ok": False, "error": f"杠杆 {leverage}x > 上限 {max_lev}x"}

    pairs = max(1, int(notional / _order_size))
    pair_notional = notional / pairs
    pair_qty = pair_notional / mid   # ccxt 的 create_order 会自动量化

    add_log(f"开仓 {symbol}: 名义 {notional:.0f}U {pairs}笔 每笔{pair_notional:.0f}U/{pair_qty:.4f}个")

    ok_pairs = 0
    total_spot = 0.0
    total_perp = 0.0
    for i in range(pairs):
        add_log(f"[{symbol}] 第 {i + 1}/{pairs} 对")
        success, spot_filled, perp_filled = execute_pair(symbol, pair_qty, _timeout)
        total_spot += spot_filled
        total_perp += perp_filled
        if success:
            ok_pairs += 1
        time.sleep(2)

    # 部分成交也登记仓位，避免幽灵仓位
    effective_qty = min(total_spot, total_perp)
    if effective_qty > 0:
        if symbol in position_state:
            pos = position_state[symbol]
            pos["qty"] += effective_qty
            pos["notional"] += effective_qty * mid
        else:
            position_state[symbol] = {
                "qty": effective_qty,
                "notional": effective_qty * mid,
                "entry_price": mid,
                "entry_time": datetime.now().isoformat(),
            }
        if abs(total_spot - total_perp) > 1e-8:
            add_log(f"  ⚠ [{symbol}] 裸敞口! 现货{total_spot:.4f}≠合约{total_perp:.4f} "
                    f"已登记{effective_qty:.4f}对仓位，差额需去 Backpack 手动处理")
    return {"ok": ok_pairs > 0 or effective_qty > 0, "pairs_done": ok_pairs, "pairs_total": pairs}


def close_position(symbol: str, order_size=None) -> dict:
    """平仓 — 以 API 真实持仓为准，不依赖 position_state 记账"""
    _order_size = order_size if order_size else ORDER_SIZE_USDC

    spot_qty, perp_qty, perp_mark = _get_real_position(symbol)
    if spot_qty <= 1e-8 and perp_qty <= 1e-8:
        return {"ok": False, "error": f"{symbol} 无持仓（API 返回现货=0 合约=0）"}

    # 合约 mark price（_get_real_position 已随 fetch_positions 一并取回，消除 N+1）
    perp_price = perp_mark if perp_mark > 0 else 1.0

    perp_notional = perp_qty * perp_price
    pairs = max(1, int(perp_notional / _order_size)) if perp_qty > 1e-8 else max(1, int(spot_qty * 100 / _order_size))
    pair_spot = spot_qty / pairs
    pair_perp = perp_qty / pairs

    add_log(f"平仓 {symbol}: API 现货{spot_qty:.4f} 合约{perp_qty:.4f} → {pairs}笔")
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

    s_rem = spot_qty - total_spot_closed
    p_rem = perp_qty - total_perp_closed
    if s_rem <= 1e-8 and p_rem <= 1e-8:
        position_state.pop(symbol, None)
        add_log(f"平仓完成 {symbol} ✓")
    else:
        add_log(f"平仓部分完成 {symbol}: 现货剩{s_rem:.4f} 合约剩{p_rem:.4f}")
        # 仍有剩余，更新 position_state 备忘（分别记现货/合约剩余，不再只记配对最小量）
        position_state[symbol] = {
            "qty": min(s_rem, p_rem),
            "spot_remaining": s_rem,
            "perp_remaining": p_rem,
            "entry_price": perp_price,
        }
    return {"ok": ok_pairs > 0, "pairs_done": ok_pairs, "pairs_total": pairs}


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
            total_assets_value = float(collateral_data.get("assetsValue", 0))
            # marginFraction 是账户级实际维持保证金率（已经是百分比数值，如 2.043 表示 2.0%）
            mf = float(collateral_data.get("marginFraction", 0))
            maintenance_margin = round(mf, 1) if mf > 0 else None
        except Exception:
            collateral_data = {}
            collateral_items = []

    # 构建持仓视图
    for pp in perp_positions:
        sym = pp["symbol"].split("/")[0]
        if not sym:
            continue
        perp_notional = abs(float(pp.get("notional", 0)))
        perp_entry = float(pp.get("entryPrice", 0))
        perp_mark = float(pp.get("markPrice", 0) or 1)
        perp_qty = abs(float(pp.get("contracts", 0)))
        perp_pnl_unrealized = round(float(pp.get("unrealizedPnl", 0)), 4)

        # 从现货抵押品中找匹配
        spot_qty = 0.0
        spot_mark = 0.0
        for ci in collateral_items:
            if ci.get("symbol") == sym:
                spot_qty = float(ci.get("totalQuantity", 0))
                spot_mark = float(ci.get("assetMarkPrice", 0))
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
            try:
                fr = ex.fetch_funding_rate(cache_key)
                funding_rate = round((fr.get("fundingRate", 0) or 0) * 24 * 365 * 100, 1)
                with _cache_lock:
                    _funding_rate_cache[cache_key] = (funding_rate, now_ts)
            except Exception:
                pass

        positions[sym] = {
            "symbol": sym,
            "spot_qty": spot_qty,
            "spot_entry": None,
            "spot_price": spot_mark,
            "spot_value": round(spot_qty * spot_mark, 2) if spot_mark else None,
            "perp_qty": perp_qty,
            "perp_entry": perp_entry,
            "perp_mark": perp_mark,
            "perp_notional": perp_notional,
            "perp_pnl_funding": 0,
            "perp_pnl_unrealized": perp_pnl_unrealized,
            "perp_pnl_realized": 0,
            "funding_rate": funding_rate,
        }

    # 扫描裸现货（有现货持仓但无合约持仓的币种，前端持仓表不可见 → 补上）
    # 只扫描有永续交易对的币种（跳过 USDC 等结算币）
    perp_tradable = {s.split("/")[0] for s in perp_symbols}
    has_perp_syms = {pp["symbol"].split("/")[0] for pp in perp_positions}
    for ci in collateral_items:
        sym = ci.get("symbol", "")
        spot_qty = float(ci.get("totalQuantity", 0))
        if spot_qty <= 1e-8 or sym in positions or sym in has_perp_syms:
            continue
        if sym not in perp_tradable:
            continue
        spot_mark = float(ci.get("assetMarkPrice", 0))
        positions[sym] = {
            "symbol": sym,
            "spot_qty": spot_qty,
            "spot_entry": None,
            "spot_price": spot_mark,
            "spot_value": round(spot_qty * spot_mark, 2) if spot_mark else None,
            "perp_qty": 0,
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
            "available": float(ci.get("availableQuantity", 0)),
            "locked": float(ci.get("openOrderQuantity", 0)),
            "lend": float(ci.get("lendQuantity", 0)),
            "total_notional": float(ci.get("balanceNotional", 0)),
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
                o["qty"] = float(o.get("amount", 0))
                o["executedQty"] = float(o.get("filled", 0))
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
        "dry_run": DRY_RUN,
        "has_key": has_key,
        "positions": positions,
        "active_orders": dict(active_orders),
        "balances": balances,
        "maintenance_margin_ratio": maintenance_margin,
        "total_assets_value": total_assets_value,
        "logs": logs,
    }


# =====================================================================
# Flask API
# =====================================================================

@app.route("/")
def index():
    return render_template("bpx_arb.html")


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
                    latest_rate = latest_apy / (24 * 365 * 100) if latest_apy else None
            if latest_apy is None:
                try:
                    fr = ex.fetch_funding_rate(cache_key)
                    latest_rate = fr.get("fundingRate", 0) or 0
                    latest_apy = round(latest_rate * 24 * 365 * 100, 1)
                    with _cache_lock:
                        _funding_rate_cache[cache_key] = (latest_apy, now_ts)
                except Exception:
                    pass
            result.append({
                "symbol": sym,
                "max_leverage": max_lev,
                "haircut": hc,
                "latest_rate": latest_rate,
                "latest_apy": round(latest_apy, 1) if latest_apy else None,
            })
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/state")
def api_state():
    """返回当前状态"""
    state = _build_state()
    return jsonify(state)


@app.route("/api/open", methods=["POST"])
def api_open():
    """开仓"""
    data = request.get_json()
    symbol = data.get("symbol", "").strip()
    notional = float(data.get("notional", 0))
    leverage = float(data.get("leverage", 1))
    order_size = float(data.get("order_size", ORDER_SIZE_USDC))
    timeout = int(data.get("timeout", PAIR_TIMEOUT_S))

    if not symbol or notional <= 0:
        return jsonify({"ok": False, "error": "币种或金额无效"}), 400

    # 费率警告
    try:
        fr = ex.fetch_funding_rate(f"{symbol}/USDC:USDC")
        current_rate = fr.get("fundingRate", 0) or 0
        current_apy = current_rate * 24 * 365 * 100
        if current_apy < MIN_WEEK_APY:
            add_log(f"[警告] {symbol} 当前年化 {current_apy:.1f}% < {MIN_WEEK_APY}%")
    except Exception:
        pass

    with _trade_lock:
        if symbol in _inflight:
            return jsonify({"ok": False, "error": f"{symbol} 有操作进行中"}), 409
        _inflight.add(symbol)

    def _run():
        try:
            open_position(symbol, notional, leverage, order_size, timeout)
        finally:
            with _trade_lock:
                _inflight.discard(symbol)

    threading.Thread(target=_run, daemon=True).start()
    add_log(f"[提交] 开仓 {symbol} {notional:.0f}USDC → 后台执行")
    return jsonify({"ok": True, "msg": "已提交后台执行"})


@app.route("/api/close", methods=["POST"])
def api_close():
    """平仓"""
    data = request.get_json()
    symbol = data.get("symbol", "").strip()
    order_size = float(data.get("order_size", ORDER_SIZE_USDC))
    if not symbol:
        return jsonify({"ok": False, "error": "币种无效"}), 400

    with _trade_lock:
        if symbol in _inflight:
            return jsonify({"ok": False, "error": f"{symbol} 有操作进行中"}), 409
        _inflight.add(symbol)

    def _run():
        try:
            close_position(symbol, order_size)
        finally:
            with _trade_lock:
                _inflight.discard(symbol)

    threading.Thread(target=_run, daemon=True).start()
    add_log(f"[提交] 平仓 {symbol} → 后台执行")
    return jsonify({"ok": True, "msg": "已提交后台执行"})


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    """撤单"""
    data = request.get_json()
    symbol = data.get("symbol", "").strip()
    if not symbol or not has_key:
        return jsonify({"ok": False, "error": "无目标或无 key"})

    try:
        spot_sym = _ccxt_spot(symbol)
        perp_sym = _ccxt_perp(symbol)
        resp_spot = ex.cancel_all_orders(spot_sym)
        resp_perp = ex.cancel_all_orders(perp_sym)
        spot_cnt = len(resp_spot) if isinstance(resp_spot, list) else 0
        perp_cnt = len(resp_perp) if isinstance(resp_perp, list) else 0
        add_log(f"手动撤单 {symbol}: 现货{spot_cnt}笔 合约{perp_cnt}笔")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# =====================================================================
# 启动
# =====================================================================

if __name__ == "__main__":
    logger.info("======== 启动 Backpack 套利面板 v4.0 (ccxt) 端口 %d ========", PORT)
    logger.info("模式: %s | 单笔: %dU | 超时: %ds | 费率阈值: %d%%",
                "DRY-RUN" if DRY_RUN else "实盘", ORDER_SIZE_USDC, PAIR_TIMEOUT_S, MIN_WEEK_APY)
    app.run(host="0.0.0.0", port=PORT, threaded=True, debug=False)
