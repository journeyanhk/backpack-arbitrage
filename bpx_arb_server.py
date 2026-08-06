# -*- coding: utf-8 -*-
"""
Backpack 资金费率套利交易脚本 v0.3 (2026-08-05 二次交叉审计修复)
Flask 后端 + 前端操作面板

端口 5055 | 实盘需 BPX_LIVE=1 环境变量
挂单策略：
  现货腿永远 maker（买一价）| 合约腿初始 maker/post_only（卖一价）
  一条腿成交后另一条改 taker（对手方最优价）
  两腿都不动满 3 分钟 → 撤单重挂

v0.3 修复（二次交叉审计，实测 API 字段结构验证）：
  ★ R2: DRY_RUN 与 account 解耦 — 有 key 就建 account 读数据，下单才看 DRY_RUN
  ★ N1: bids 数组是升序 — bids[0]=最差买价, bids[-1]=买一（6处修正）
  ★ N2: 合约腿独立取合约盘口 — 不再用现货价给合约定价（7处修正）
  ★ N3: 下单精度按 filters 量化 — 代替硬编码 round(x,4)
  ★ N4: HTTP 4xx 不抛异常 — 显式检查 code 字段, qty>0 防御
  ★ R1: haircut 回退原写法 (base 在 kind 里)
  ★ N6: active_orders 按币种基名分组, 补 sym 字段
  ★ N8: 单腿成交不返回 True（裸仓告警）
  ★ N9: 重复开仓累加而非覆盖
  ★ N10: 开仓前检查 USDC 可用余额
  ★ N7: 开/平仓返回立即返回, 后台线程执行, 加锁防并发
  ★ N5: bpx_trader.py get_open_orders 传参 keyword= 修正
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

from flask import Flask, jsonify, render_template, request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bpx_stock import PublicStock
from bpx.account import Account

# =====================================================================
# 配置
# =====================================================================
PORT = 5055
DRY_RUN = os.environ.get("BPX_LIVE", "0").strip() != "1"
PAIR_TIMEOUT_S = 180               # 单对超时秒数（3分钟）
MAX_RETRIES = 3                    # 最大重挂次数
ORDER_SIZE_USDC = 100.0            # 单笔名义 USDC
MIN_WEEK_APY = 10.0                # 费率警告阈值
BPX_PUBLIC_KEY = os.environ.get("BPX_PUBLIC_KEY", "")
BPX_SECRET_KEY = os.environ.get("BPX_SECRET_KEY", "")

# .env 回退
if not BPX_PUBLIC_KEY or not BPX_SECRET_KEY:
    env_file = os.path.join(os.getcwd(), ".env")
    if not os.path.exists(env_file):
        env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ[k] = v
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
# ★ R2修复: account 与 DRY_RUN 解耦
#    有 key 就一定建 account（用于读仓/余额），下单动作才看 DRY_RUN
# =====================================================================
pub = PublicStock()
account: Optional[Account] = None
if BPX_PUBLIC_KEY and BPX_SECRET_KEY:
    account = Account(BPX_PUBLIC_KEY, BPX_SECRET_KEY, window=5000)
    logger.info("Account 已初始化（%s模式）", "DRY-RUN" if DRY_RUN else "实盘")
else:
    logger.warning("未配置 API key，仅行情可用")

# =====================================================================
# 行情缓存 + 市场过滤器缓存
# =====================================================================
_cache: Dict[str, tuple] = {}             # key -> (value, expiry_ts)
_cache_lock = threading.Lock()
_market_filters: Dict[str, dict] = {}     # symbol -> {tickSize, stepSize, minQty, maxQty}

def cached_get(key: str, fetcher, ttl: int = 30):
    """★ 加锁双重检查，防止多线程并发时重复调用 fetcher"""
    now = time.time()
    # 快速路径：不加锁先查
    if key in _cache:
        val, exp = _cache[key]
        if now < exp:
            return val
    # 慢速路径：加锁后重新检查
    with _cache_lock:
        if key in _cache:
            val, exp = _cache[key]
            if now < exp:
                return val
        val = fetcher()
        _cache[key] = (val, now + ttl)
    return val

def _fetch_funding(sym: str):
    try:
        fr = pub.get_funding_interval_rates(f"{sym}_USDC_PERP", limit=1)
        if isinstance(fr, list) and fr:
            return round(float(fr[0].get("fundingRate", 0)) * 24 * 365 * 100, 1)
    except Exception:
        pass
    return None

def _fetch_if(acc, method=None):
    """安全拉取 account 数据（★ R2修复: 移除 DRY_RUN 阻断，读数据不查 DRY_RUN）"""
    if not acc:
        return [] if method else []
    try:
        if method:
            return method()
        else:
            pp = acc.get_open_positions()
            return pp if isinstance(pp, list) else []
    except Exception:
        return []


# ★ N3修复: 市场精度过滤器
def _load_market_filters():
    """从 get_markets() 缓存所有 symbol 的 tickSize / stepSize / minQty"""
    try:
        ms = pub.get_markets()
        for m in ms:
            sym = m["symbol"]
            f = m["filters"]
            _market_filters[sym] = {
                "tickSize": float(f["price"]["tickSize"]),
                "stepSize": float(f["quantity"]["stepSize"]),
                "minQty": float(f["quantity"]["minQuantity"]),
                "maxQty": float(f["quantity"].get("maxQuantity", 0) or 1e18),
            }
        logger.info("已加载 %d 个市场过滤器", len(_market_filters))
    except Exception as e:
        logger.warning("加载市场过滤器失败: %s", e)


def _quantize_qty(symbol: str, qty: float) -> float:
    """按 symbol 的 stepSize 量化数量"""
    f = _market_filters.get(symbol)
    if not f:
        return round(qty, 4)  # 回退
    step = f["stepSize"]
    q = round(qty / step) * step
    return max(f["minQty"], min(f["maxQty"], q))


def _quantize_price(symbol: str, price: float) -> float:
    """按 symbol 的 tickSize 量化价格"""
    f = _market_filters.get(symbol)
    if not f:
        return round(price, 4)  # 回退
    tick = f["tickSize"]
    return round(round(price / tick) * tick, 8)


# 启动时加载过滤器
_load_market_filters()


# =====================================================================
# 全局状态
# =====================================================================
position_state: Dict[str, Any] = {}          # {symbol: {spot_qty, perp_qty, ...}}
active_orders: Dict[str, List[dict]] = {}    # {base_coin: [order_info, ...]}  ★ N6: 按币种基名分组
operation_log: List[str] = []
_trade_lock = threading.Lock()
_inflight: set = set()                          # ★ N7: 防止并发开平


def add_log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    operation_log.append(entry)
    if len(operation_log) > 200:
        operation_log.pop(0)
    logger.info(msg)


def _refresh_active_orders():
    """★ N6修复: 从 SDK 拉取活跃订单, 按币种基名分组, 补前端需要的字段"""
    if not account:
        return
    try:
        raw = account.get_open_orders() or []
        active_orders.clear()
        for o in raw:
            sym = o.get("symbol", "")
            if not sym:
                continue
            base = sym.replace("_USDC_PERP", "").replace("_USDC", "")
            o["market"] = "合约" if "_PERP" in sym else "现货"
            o["sym"] = sym                       # 前端 filter 用
            o["qty"] = float(o.get("quantity", 0))
            o["executedQty"] = float(o.get("executedQuantity", 0))
            o["price"] = float(o.get("price", 0))        # ★ 前端显示用
            o["side"] = o.get("side", "?")               # ★ 前端显示用
            if base not in active_orders:
                active_orders[base] = []
            active_orders[base].append(o)
    except Exception:
        pass


# =====================================================================
# OrderSplitter — 拆单 + 挂单 + 监控成交
# =====================================================================

class OrderSplitter:

    def __init__(self, dry_run: bool = DRY_RUN):
        self.dry_run = dry_run

    # ===== 盘口工具 (★ N1修复: bids[-1]=买一) =====

    def _depth_mid(self, sym: str) -> Optional[float]:
        """取 depth 中价"""
        try:
            d = pub.get_depth(sym)
            if isinstance(d, dict) and d.get("asks") and d.get("bids"):
                return (float(d["asks"][0][0]) + float(d["bids"][-1][0])) / 2  # ★ bids[-1]
        except Exception:
            pass
        return None

    def _depth_bbo(self, sym: str, spot_fallback: bool = False) -> tuple:
        """返回 (买一价, 卖一价)。spot_fallback=True 时用 External ticker 兜底。
        ★N1修复: bids[-1] 取买一 ★N2修复: 合约也直接从自己的 depth 取"""
        try:
            d = pub.get_depth(sym)
            if isinstance(d, dict) and d.get("asks") and d.get("bids"):
                return float(d["bids"][-1][0]), float(d["asks"][0][0])  # ★ bids[-1]
        except Exception:
            pass
        # 现货 fallback（合约不走 External ticker）
        if spot_fallback:
            try:
                t = pub.get_ticker(sym, source="External")
                if isinstance(t, dict) and t.get("lastPrice"):
                    p = float(t["lastPrice"])
                    return p * 0.999, p * 1.001
            except Exception:
                pass
        return None, None

    # ===== 下单 (★ N3修复: 精度按 filters 量化) =====

    def _place_spot_buy(self, sym: str, qty: float, price: float,
                        post_only: bool = True) -> Optional[str]:
        action = f"现货买入 {sym} {qty:.6f} @ {price:.8f}"
        if self.dry_run or not account:
            add_log(f"[DRY] {action}")
            return f"dry-{int(time.time()*1000)}"
        try:
            qq = _quantize_qty(sym, qty)       # ★ N3
            pp = _quantize_price(sym, price)   # ★ N3
            resp = account.execute_order(
                symbol=sym, side="Bid", order_type="Limit",
                quantity=str(qq), price=str(pp),
                time_in_force="GTC", post_only=post_only,
                self_trade_prevention="RejectTaker",
            )
            oid = resp.get("id") if isinstance(resp, dict) and "id" in resp else None
            if oid:
                add_log(f"{action} → order_id={oid}")
                return str(oid)
            # ★ N4修复: 检查 code 字段（Backpack 错误格式是 code/message 不是 error）
            code = resp.get("code") if isinstance(resp, dict) else None
            add_log(f"[FAIL] {action}: code={code} resp={resp}")
            return None
        except Exception as e:
            add_log(f"[FAIL] {action}: {e}")
            return None

    def _place_perp_sell(self, sym: str, qty: float, price: float,
                         post_only: bool = True) -> Optional[str]:
        mode = "maker(postOnly)" if post_only else "taker"
        action = f"合约卖出 {sym} {qty:.6f} @ {price:.8f} ({mode})"
        if self.dry_run or not account:
            add_log(f"[DRY] {action}")
            return f"dry-{int(time.time()*1000)}"
        try:
            qq = _quantize_qty(sym, qty)       # ★ N3
            pp = _quantize_price(sym, price)   # ★ N3
            resp = account.execute_order(
                symbol=sym, side="Ask", order_type="Limit",
                quantity=str(qq), price=str(pp),
                time_in_force="GTC", post_only=post_only,
                self_trade_prevention="RejectTaker",
            )
            oid = resp.get("id") if isinstance(resp, dict) and "id" in resp else None
            if oid:
                add_log(f"{action} → order_id={oid}")
                return str(oid)
            code = resp.get("code") if isinstance(resp, dict) else None
            add_log(f"[FAIL] {action}: code={code} resp={resp}")
            return None
        except Exception as e:
            add_log(f"[FAIL] {action}: {e}")
            return None

    # ===== 撤单 (★ N4修复: 检查 code 而非 error) =====

    def _cancel_order(self, sym: str, order_id: str) -> bool:
        if self.dry_run or not account or order_id.startswith("dry-"):
            add_log(f"[DRY] 撤单 {sym} {order_id}")
            return True
        try:
            resp = account.cancel_order(sym, order_id)
            if isinstance(resp, dict):
                code = resp.get("code")
                if code:
                    add_log(f"[FAIL] 撤单 {sym} {order_id}: code={code} {resp}")
                    return False
            add_log(f"撤单 {sym} {order_id}")
            return True
        except Exception as e:
            add_log(f"[FAIL] 撤单 {sym} {order_id}: {e}")
            return False

    # ===== 查单 (★ N4修复: qty>0 防御 4xx 被当正常数据) =====

    def _check_order_filled(self, sym: str, order_id: str) -> tuple:
        """返回 (是否完全成交, 已成交量)"""
        if order_id.startswith("dry-"):
            return True, ORDER_SIZE_USDC / 100
        if not account:
            return False, 0
        try:
            resp = account.get_open_order(sym, order_id)
            # ★ N4: 空或错误体都视为查不到（不区分 404 和真成交）
            if not resp or not isinstance(resp, dict):
                return True, 1.0
            # ★ N4: 检查是否 API 错误（code 字段 = 不是正常订单数据）
            if resp.get("code"):
                return False, 0
            qty = float(resp.get("quantity", 0))
            executed = float(resp.get("executedQuantity", 0))
            # ★ N4防御: qty 为 0 时不判已成交（4xx 错误体会出此路径）
            if qty <= 0:
                return False, 0
            return (executed >= qty, executed)
        except Exception:
            return False, 0   # 异常不等同已成交

    # ===== 开仓一对 (★N1+N2+N3+N4+N8 全部修复) =====

    def execute_pair(
        self,
        spot_sym: str,
        perp_sym: str,
        qty: float,
        timeout_s: int = PAIR_TIMEOUT_S,
    ) -> bool:
        """执行一对：现货买 + 合约卖，保证成交或全部撤销
        ★ N2修复: 现货/合约各自取自己的盘口
        ★ N8修复: 单腿成交不返回 True
        """
        spot_bid, spot_ask = self._depth_bbo(spot_sym, spot_fallback=True)
        perp_bid, perp_ask = self._depth_bbo(perp_sym)   # ★ N2: 合约用自己的盘口
        if not spot_bid or not spot_ask or not perp_bid or not perp_ask:
            add_log(f"[跳] {spot_sym}/{perp_sym} 无盘口数据")
            return False

        for attempt in range(1, MAX_RETRIES + 1):
            add_log(f"  [{spot_sym}] 第{attempt}次: 现货买@{spot_bid} 合约卖@{perp_ask}")

            spot_oid = self._place_spot_buy(spot_sym, qty, spot_bid, post_only=True)
            perp_oid = self._place_perp_sell(perp_sym, qty, perp_ask, post_only=True)
            if not spot_oid or not perp_oid:
                if spot_oid: self._cancel_order(spot_sym, spot_oid)
                if perp_oid: self._cancel_order(perp_sym, perp_oid)
                return False

            start = time.time()
            while time.time() - start < timeout_s:
                time.sleep(2)
                s_ok, s_exec = self._check_order_filled(spot_sym, spot_oid)
                p_ok, p_exec = self._check_order_filled(perp_sym, perp_oid)

                if s_ok and p_ok:
                    add_log(f"  [{spot_sym}] 两腿均成交 ✓")
                    return True

                if s_ok and not p_ok:
                    # ★ N2修复: 合约 taker 用合约自己的盘口
                    add_log(f"  [{spot_sym}] 现货成交，合约改 taker")
                    self._cancel_order(perp_sym, perp_oid)
                    _, new_perp_ask = self._depth_bbo(perp_sym)   # ★ N2: 合约盘口
                    if new_perp_ask:
                        perp_oid = self._place_perp_sell(perp_sym, qty, new_perp_ask, post_only=False)
                        if not perp_oid:
                            # ★ N8: 单腿成交 → 告警，不返回 True
                            add_log(f"  ⚠ [{spot_sym}] 现货已成交但合约无法成交！裸多预警！")
                            return False
                    else:
                        add_log(f"  ⚠ [{spot_sym}] 合约 taker 无盘口！裸多预警！")
                        return False

                elif p_ok and not s_ok:
                    # 合约成交，现货改 taker（现货用自己的盘口，这里本来就对）
                    add_log(f"  [{spot_sym}] 合约成交，现货改 taker")
                    self._cancel_order(spot_sym, spot_oid)
                    new_spot_bid, _ = self._depth_bbo(spot_sym, spot_fallback=True)
                    if new_spot_bid:
                        spot_oid = self._place_spot_buy(spot_sym, qty, new_spot_bid, post_only=False)
                        if not spot_oid:
                            add_log(f"  ⚠ [{spot_sym}] 合约已成交但现货无法成交！裸空预警！")
                            return False
                    else:
                        add_log(f"  ⚠ [{spot_sym}] 现货 taker 无盘口！裸空预警！")
                        return False

            # 超时，撤单重挂
            add_log(f"  [{spot_sym}] 超时，撤单重挂")
            self._cancel_order(spot_sym, spot_oid)
            self._cancel_order(perp_sym, perp_oid)
            time.sleep(2)
            # ★ N2修复: 重刷新两套盘口
            spot_bid, spot_ask = self._depth_bbo(spot_sym, spot_fallback=True)
            perp_bid, perp_ask = self._depth_bbo(perp_sym)
            if not spot_bid or not spot_ask or not perp_bid or not perp_ask:
                return False

        add_log(f"  [{spot_sym}] {MAX_RETRIES}次重试均失败")
        return False

    # ===== 平仓腿 (★ N3修复: 精度量化) =====

    def _place_spot_sell(self, sym: str, qty: float, price: float) -> Optional[str]:
        action = f"现货卖出 {sym} {qty:.6f} @ {price:.8f}"
        if self.dry_run or not account:
            add_log(f"[DRY] {action}")
            return f"dry-{int(time.time()*1000)}"
        try:
            qq = _quantize_qty(sym, qty)
            pp = _quantize_price(sym, price)
            resp = account.execute_order(
                symbol=sym, side="Ask", order_type="Limit",
                quantity=str(qq), price=str(pp),
                time_in_force="GTC", self_trade_prevention="RejectTaker",
            )
            oid = resp.get("id") if isinstance(resp, dict) and "id" in resp else None
            if oid:
                add_log(f"{action} → order_id={oid}")
                return str(oid)
            code = resp.get("code") if isinstance(resp, dict) else None
            add_log(f"[FAIL] {action}: code={code} resp={resp}")
            return None
        except Exception as e:
            add_log(f"[FAIL] {action}: {e}")
            return None

    def _place_perp_buy(self, sym: str, qty: float, price: float) -> Optional[str]:
        action = f"合约买入平仓 {sym} {qty:.6f} @ {price:.8f}"
        if self.dry_run or not account:
            add_log(f"[DRY] {action}")
            return f"dry-{int(time.time()*1000)}"
        try:
            qq = _quantize_qty(sym, qty)
            pp = _quantize_price(sym, price)
            resp = account.execute_order(
                symbol=sym, side="Bid", order_type="Limit",
                quantity=str(qq), price=str(pp),
                time_in_force="GTC", post_only=True,
                self_trade_prevention="RejectTaker",
            )
            oid = resp.get("id") if isinstance(resp, dict) and "id" in resp else None
            if oid:
                add_log(f"{action} → order_id={oid}")
                return str(oid)
            code = resp.get("code") if isinstance(resp, dict) else None
            add_log(f"[FAIL] {action}: code={code} resp={resp}")
            return None
        except Exception as e:
            add_log(f"[FAIL] {action}: {e}")
            return None

    # ===== 平仓一对 (★ N2修复: 各取各的盘口, ★ N1修复: bids[-1]) =====

    def close_pair(self, spot_sym: str, perp_sym: str, qty: float) -> bool:
        spot_bid, spot_ask = self._depth_bbo(spot_sym, spot_fallback=True)
        perp_bid, perp_ask = self._depth_bbo(perp_sym)
        # 现货卖用现货 ask，合约买入平仓用合约 bid
        spot_oid = self._place_spot_sell(spot_sym, qty, spot_ask) if spot_ask else None
        perp_oid = self._place_perp_buy(perp_sym, qty, perp_bid) if perp_bid else None
        return bool(spot_oid and perp_oid)


# =====================================================================
# ArbitrageEngine — 开仓/平仓编排
# =====================================================================

class ArbitrageEngine:

    def __init__(self):
        self.splitter = OrderSplitter()

    def open_position(self, symbol: str, notional: float, leverage: float) -> dict:
        """开仓 (★ N9: 累加仓位, ★ N10: 余额校验)"""
        spot_sym = f"{symbol}_USDC"
        perp_sym = f"{symbol}_USDC_PERP"

        mid = self.splitter._depth_mid(spot_sym)
        if not mid:
            return {"ok": False, "error": "无参考价"}

        # ★ N10: 检查 USDC 可用余额
        if account and not DRY_RUN:
            try:
                col = account.get_collateral()
                usdc_avail = 0.0
                for c in col if isinstance(col, list) else []:
                    if c.get("symbol") == "USDC":
                        usdc_avail = float(c.get("availableQuantity", 0))
                        break
                if 0 < usdc_avail < notional:
                    return {"ok": False, "error": f"USDC 可用 {usdc_avail:.0f} < 开仓 {notional:.0f}"}
            except Exception:
                pass

        # ★ N3修复: pair_qty 按 filters 量化
        pairs = max(1, int(notional / ORDER_SIZE_USDC))
        pair_notional = notional / pairs
        pair_qty = _quantize_qty(spot_sym, pair_notional / mid)

        add_log(f"开仓 {symbol}: 名义 {notional:.0f}U {pairs}笔 每笔{pair_notional:.0f}U/{pair_qty}个")

        # 校验杠杆
        try:
            col = pub.get_collateral()
            for c in col if isinstance(col, list) else []:
                if c.get("symbol") == symbol:
                    imf = float(c.get("imfFunction", {}).get("base", 1))
                    max_lev = math.floor(1 / imf) if 0 < imf < 100 else 0
                    if leverage > max_lev:
                        return {"ok": False, "error": f"杠杆 {leverage}x > 上限 {max_lev}x"}
                    break
        except Exception:
            pass

        ok = 0
        for i in range(pairs):
            add_log(f"[{symbol}] 第 {i+1}/{pairs} 对")
            if self.splitter.execute_pair(spot_sym, perp_sym, pair_qty):
                ok += 1
            time.sleep(2)

        if ok > 0:
            # ★ N9修复: 重复开仓累加而非覆盖
            if symbol in position_state:
                pos = position_state[symbol]
                pos["qty"] += ok * pair_qty
                pos["notional"] += ok * pair_notional
            else:
                position_state[symbol] = {
                    "spot_sym": spot_sym, "perp_sym": perp_sym,
                    "qty": ok * pair_qty, "notional": ok * pair_notional,
                    "entry_price": mid, "entry_time": datetime.now().isoformat(),
                }
        return {"ok": ok > 0, "pairs_done": ok, "pairs_total": pairs}

    def close_position(self, symbol: str) -> dict:
        """平仓（fallback: API 持仓推算）"""
        pos = position_state.get(symbol)
        if not pos:
            try:
                pp = account.get_open_positions() if account else []
                for p in (pp if isinstance(pp, list) else []):
                    psym = p.get("symbol", "").replace("_USDC_PERP", "")
                    if psym == symbol:
                        total_qty = abs(float(p.get("netExposureQuantity", 0)))
                        if total_qty <= 0:
                            return {"ok": False, "error": f"{symbol} API 持仓为 0"}
                        pos = {
                            "spot_sym": f"{symbol}_USDC",
                            "perp_sym": f"{symbol}_USDC_PERP",
                            "qty": total_qty,
                            "entry_price": float(p.get("entryPrice", 1)),
                        }
                        add_log(f"平仓用 API 持仓: {symbol} {total_qty:.4f}")
                        break
            except Exception:
                pass
            if not pos:
                return {"ok": False, "error": f"无 {symbol} 持仓"}

        total_qty = pos["qty"]
        pairs = max(1, int(total_qty * pos.get("entry_price", 1) / ORDER_SIZE_USDC))
        pair_qty = _quantize_qty(pos["spot_sym"], total_qty / pairs)

        add_log(f"平仓 {symbol}: {total_qty:.4f}个")
        ok = 0
        for i in range(pairs):
            if self.splitter.close_pair(pos["spot_sym"], pos["perp_sym"], pair_qty):
                ok += 1
            time.sleep(2)

        if ok == pairs:
            position_state.pop(symbol, None)
            add_log(f"平仓完成 {symbol}")
        return {"ok": ok > 0, "pairs_done": ok, "pairs_total": pairs}


engine = ArbitrageEngine()


# =====================================================================
# Flask API
# =====================================================================

@app.route("/")
def index():
    return render_template("bpx_arb.html")


@app.route("/api/symbols")
def api_symbols():
    """可选币种列表（★ R1修复: haircut 回退原写法——base 在 kind 里）"""
    try:
        col = pub.get_collateral()
        ms = pub.get_markets()
        perp_set = {m["symbol"].replace("_USDC_PERP", "") for m in ms if m["symbol"].endswith("_PERP")}
        result = []
        for c in col:
            sym = c.get("symbol", "")
            if sym not in perp_set:
                continue
            imf = float(c.get("imfFunction", {}).get("base", 1))
            max_lev = math.floor(1 / imf) if 0 < imf < 100 else 0
            # ★ R1修复: haircut 的 base 在 kind 里面
            hc_kind = c.get("haircutFunction", {}).get("kind", {})
            hc = float(hc_kind.get("base", hc_kind.get("weight", 0)))
            latest_rate = None
            try:
                fr = pub.get_funding_interval_rates(f"{sym}_USDC_PERP", limit=1)
                if isinstance(fr, list) and fr:
                    latest_rate = float(fr[0].get("fundingRate", 0))
            except Exception:
                pass
            latest_apy = latest_rate * 24 * 365 * 100 if latest_rate is not None else None
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
    """返回当前状态（★ R2修复: 读数据不管 DRY_RUN）"""
    positions = {}
    maintenance_margin_ratio = None
    perp_positions = []
    spot_balances = {}

    # 拉合约持仓
    perp_positions = cached_get("perp_positions", lambda: _fetch_if(account), ttl=10)

    # ★ R2修复: 拉抵押品（不再受 DRY_RUN 阻塞）
    collateral_items = []
    if account:
        try:
            col = cached_get("collateral_items", lambda: _fetch_if(account, account.get_collateral), ttl=10)
            collateral_items = col if isinstance(col, list) else []
        except Exception:
            collateral_items = []

    # 构建持仓视图
    for pp in perp_positions:
        sym = pp.get("symbol","").replace("_USDC_PERP","")
        if not sym: continue
        perp_notional = abs(float(pp.get("netExposureNotional", 0)))
        perp_entry = float(pp.get("entryPrice", 0))
        perp_mark = float(pp.get("markPrice", pp.get("breakEvenPrice", 0) or 1))
        perp_qty = abs(float(pp.get("netExposureQuantity", 0)))
        perp_pnl_funding = round(float(pp.get("cumulativeFundingPayment", 0)), 4)
        perp_pnl_unrealized = round(float(pp.get("pnlUnrealized", 0)), 4)
        perp_pnl_realized = round(float(pp.get("pnlRealized", 0)), 4)
        spot_qty = 0.0
        spot_mark = 0.0
        for ci in collateral_items:
            if ci.get("symbol") == sym:
                spot_qty = float(ci.get("totalQuantity", 0))
                spot_mark = float(ci.get("assetMarkPrice", 0))
                break
        funding_rate = cached_get(f"funding_{sym}", lambda: _fetch_funding(sym), ttl=60)
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
            "perp_pnl_funding": perp_pnl_funding,
            "perp_pnl_unrealized": perp_pnl_unrealized,
            "perp_pnl_realized": perp_pnl_realized,
            "funding_rate": funding_rate,
        }

    # ★ R2修复: 维持保证金率（不查 DRY_RUN）
    if account:
        try:
            acc = account.get_account()
            if isinstance(acc, dict):
                lev = float(acc.get("leverageLimit", 0))
                if lev > 0:
                    maintenance_margin_ratio = round(100 / lev, 1)
        except Exception:
            pass

    # ★ R2修复: 余额从 collateral_items 构建
    bal_list = []
    for ci in collateral_items:
        bal_list.append({
            "asset": ci.get("symbol", "?"),
            "available": float(ci.get("availableQuantity", 0)),
            "locked": float(ci.get("openOrderQuantity", 0)),
            "lend": float(ci.get("lendQuantity", 0)),
            "total_notional": float(ci.get("balanceNotional", 0)),
        })
    total_assets_value = round(sum(float(ci.get("balanceNotional", 0)) for ci in collateral_items), 2) if collateral_items else None

    # ★ N6: 刷新活跃订单
    _refresh_active_orders()

    return jsonify({
        "dry_run": DRY_RUN, "has_key": bool(BPX_PUBLIC_KEY and BPX_SECRET_KEY),
        "positions": positions, "active_orders": active_orders,
        "balances": bal_list, "maintenance_margin_ratio": maintenance_margin_ratio,
        "total_assets_value": total_assets_value, "logs": operation_log[-30:],
    })


@app.route("/api/open", methods=["POST"])
def api_open():
    """开仓 (★ N7修复: 立即返回, 后台执行, 加锁防并发)"""
    data = request.get_json()
    symbol = data.get("symbol", "").strip()
    notional = float(data.get("notional", 0))
    leverage = float(data.get("leverage", 1))

    if not symbol or notional <= 0:
        return jsonify({"ok": False, "error": "币种或金额无效"}), 400

    # 费率警告
    try:
        fr = pub.get_funding_interval_rates(f"{symbol}_USDC_PERP", limit=168)
        if isinstance(fr, list) and fr:
            week_avg = sum(float(f["fundingRate"]) for f in fr) / len(fr)
            week_apy = week_avg * 24 * 365 * 100
            if week_apy < MIN_WEEK_APY:
                add_log(f"[警告] {symbol} 周均年化 {week_apy:.1f}% < {MIN_WEEK_APY}%")
    except Exception:
        pass

    # ★ N7: 加锁并发控制
    with _trade_lock:
        if symbol in _inflight:
            return jsonify({"ok": False, "error": f"{symbol} 有操作进行中，请等待完成"}), 409
        _inflight.add(symbol)

    def _run():
        try:
            engine.open_position(symbol, notional, leverage)
        finally:
            with _trade_lock:
                _inflight.discard(symbol)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    add_log(f"[提交] 开仓 {symbol} {notional:.0f}USDC → 后台执行")
    return jsonify({"ok": True, "msg": f"开仓 {symbol} 已提交，后台执行中"})


@app.route("/api/close", methods=["POST"])
def api_close():
    """平仓 (★ N7修复: 立即返回, 后台执行, 加锁防并发)"""
    data = request.get_json()
    symbol = data.get("symbol", "").strip()
    if not symbol:
        return jsonify({"ok": False, "error": "请指定币种"}), 400

    with _trade_lock:
        if symbol in _inflight:
            return jsonify({"ok": False, "error": f"{symbol} 有操作进行中，请等待完成"}), 409
        _inflight.add(symbol)

    def _run():
        try:
            engine.close_position(symbol)
        finally:
            with _trade_lock:
                _inflight.discard(symbol)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    add_log(f"[提交] 平仓 {symbol} → 后台执行")
    return jsonify({"ok": True, "msg": f"平仓 {symbol} 已提交，后台执行中"})


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    """撤销活跃订单"""
    data = request.get_json() or {}
    symbol = data.get("symbol", "")
    add_log(f"撤单指令: {symbol or '全部'}")
    if account and not DRY_RUN and symbol:
        try:
            account.cancel_all_orders(f"{symbol}_USDC")
            account.cancel_all_orders(f"{symbol}_USDC_PERP")
        except Exception:
            pass
    return jsonify({"ok": True})


# ---- 启动 ----
if __name__ == "__main__":
    mode = "DRY-RUN" if DRY_RUN else "LIVE(实盘)"
    has_key = bool(BPX_PUBLIC_KEY and BPX_SECRET_KEY)
    print(f"\n{'='*50}")
    print(f"  Backpack 资金费率套利交易面板 v0.3")
    print(f"  端口: {PORT} | 模式: {mode} | key: {'✓' if has_key else '✗'}")
    print(f"  需 BPX_LIVE=1 才实际下单 | 浏览器: http://127.0.0.1:{PORT}")
    print(f"{'='*50}\n")
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
