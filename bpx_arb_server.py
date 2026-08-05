# -*- coding: utf-8 -*-
"""
Backpack 资金费率套利交易脚本 v0.1 (2026-08-05)
Flask 后端 + 前端操作面板

端口 6000 | 所有操作手动触发 | dry_run 模式默认开启
挂单策略：
  现货腿永远 maker（买一价）| 合约腿初始 maker/post_only（卖一价）
  一条腿成交后另一条改 taker（对手方最优价）
  两腿都不动满 3 分钟 → 撤单重挂
"""
import json
import logging
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

# ---- 配置 ----
PORT = 5055
DRY_RUN = True                     # 默认演练模式
PAIR_TIMEOUT_S = 180               # 单对超时秒数（3分钟）
MAX_RETRIES = 3                    # 最大重挂次数
ORDER_SIZE_USDC = 100.0            # 单笔名义 USDC
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

# ---- 全局状态 ----
pub = PublicStock()
account: Optional[Account] = None
if not DRY_RUN and BPX_PUBLIC_KEY and BPX_SECRET_KEY:
    account = Account(BPX_PUBLIC_KEY, BPX_SECRET_KEY, window=5000)

position_state: Dict[str, Any] = {}   # {symbol: {spot_qty, perp_qty, open_price, ...}}
active_orders: Dict[str, List[dict]] = {}  # {symbol: [order_info, ...]}
operation_log: List[str] = []


def add_log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    operation_log.append(entry)
    if len(operation_log) > 200:
        operation_log.pop(0)
    logger.info(msg)


# ==================== OrderSplitter ====================

class OrderSplitter:
    """拆单 + 挂单 + 监控成交 + 撤单重挂"""

    def __init__(self, dry_run: bool = DRY_RUN):
        self.dry_run = dry_run

    def _depth_mid(self, spot_sym: str) -> Optional[float]:
        """取现货 depth 中价"""
        try:
            d = pub.get_depth(spot_sym)
            if isinstance(d, dict) and d.get("asks") and d.get("bids"):
                return (float(d["asks"][0][0]) + float(d["bids"][0][0])) / 2
        except Exception:
            pass
        return None

    def _depth_bbo(self, spot_sym: str) -> tuple:
        """返回 (买一价, 卖一价)，失败时用 external ticker"""
        try:
            d = pub.get_depth(spot_sym)
            if isinstance(d, dict) and d.get("asks") and d.get("bids"):
                return float(d["bids"][0][0]), float(d["asks"][0][0])
        except Exception:
            pass
        try:
            t = pub.get_ticker(spot_sym, source="External")
            if isinstance(t, dict) and t.get("lastPrice"):
                p = float(t["lastPrice"])
                return p * 0.999, p * 1.001
        except Exception:
            pass
        return None, None

    def _place_spot_buy(self, sym: str, qty: float, price: float) -> Optional[str]:
        """现货限价买单，挂买一价"""
        action = f"现货买入 {sym} {qty:.4f} @ {price:.4f}"
        if self.dry_run or not account:
            add_log(f"[DRY] {action}")
            return f"dry-{int(time.time()*1000)}"
        try:
            cfg = account.execute_order(
                symbol=sym, side="Bid", order_type="Limit",
                quantity=str(round(qty, 4)), price=str(round(price, 4)),
                time_in_force="GTC", self_trade_prevention="RejectTaker",
            )
            resp = account.http_client.post(cfg.url, headers=cfg.headers, data=cfg.data)
            oid = resp.get("id") if isinstance(resp, dict) else str(resp)[:20]
            add_log(f"{action} → order_id={oid}")
            return str(oid)
        except Exception as e:
            add_log(f"[FAIL] {action}: {e}")
            return None

    def _place_perp_sell(self, sym: str, qty: float, price: float, post_only: bool = True) -> Optional[str]:
        """合约卖单，post_only=True 时为 maker"""
        mode = "maker(postOnly)" if post_only else "taker"
        action = f"合约卖出 {sym} {qty:.4f} @ {price:.4f} ({mode})"
        if self.dry_run or not account:
            add_log(f"[DRY] {action}")
            return f"dry-{int(time.time()*1000)}"
        try:
            cfg = account.execute_order(
                symbol=sym, side="Ask", order_type="Limit",
                quantity=str(round(qty, 4)), price=str(round(price, 4)),
                time_in_force="GTC", post_only=post_only,
                self_trade_prevention="RejectTaker",
            )
            resp = account.http_client.post(cfg.url, headers=cfg.headers, data=cfg.data)
            oid = resp.get("id") if isinstance(resp, dict) else str(resp)[:20]
            add_log(f"{action} → order_id={oid}")
            return str(oid)
        except Exception as e:
            add_log(f"[FAIL] {action}: {e}")
            return None

    def _cancel_order(self, sym: str, order_id: str):
        """撤单"""
        if self.dry_run or not account or order_id.startswith("dry-"):
            add_log(f"[DRY] 撤单 {sym} {order_id}")
            return True
        try:
            cfg = account.cancel_order(sym, order_id)
            account.http_client.delete(cfg.url, headers=cfg.headers, data=cfg.data)
            add_log(f"撤单 {sym} {order_id}")
            return True
        except Exception as e:
            add_log(f"[FAIL] 撤单 {sym} {order_id}: {e}")
            return False

    def _check_order_filled(self, sym: str, order_id: str) -> tuple:
        """返回 (是否完全成交, 已成交量)"""
        if order_id.startswith("dry-"):
            return True, ORDER_SIZE_USDC / 100  # dry-run 模拟成交
        if not account:
            return False, 0
        try:
            cfg = account.get_open_order(sym, order_id)
            resp = account.http_client.get(cfg.url, headers=cfg.headers)
            if not resp:
                return True, 1.0  # 查不到 = 已成交
            qty = float(resp.get("quantity", 0))
            executed = float(resp.get("executedQuantity", 0))
            return (executed >= qty, executed)
        except Exception:
            return True, 0

    def execute_pair(
        self,
        spot_sym: str,    # e.g. MON_USDC
        perp_sym: str,    # e.g. MON_USDC_PERP
        qty: float,       # 币的数量
        timeout_s: int = PAIR_TIMEOUT_S,
    ) -> bool:
        """执行一对：现货买 + 合约卖，保证成交或全部撤销"""
        bid, ask = self._depth_bbo(spot_sym)
        if not bid or not ask:
            add_log(f"[跳] {spot_sym} 无盘口数据")
            return False

        for attempt in range(1, MAX_RETRIES + 1):
            add_log(f"  [{spot_sym}] 第{attempt}次尝试: 现货@{bid} 合约@{ask}")

            # 挂两腿
            spot_oid = self._place_spot_buy(spot_sym, qty, bid)
            perp_oid = self._place_perp_sell(perp_sym, qty, ask, post_only=True)
            if not spot_oid or not perp_oid:
                if spot_oid: self._cancel_order(spot_sym, spot_oid)
                if perp_oid: self._cancel_order(perp_sym, perp_oid)
                return False

            # 等待成交
            start = time.time()
            spot_done = perp_done = False
            while time.time() - start < timeout_s:
                time.sleep(2)
                s_ok, s_exec = self._check_order_filled(spot_sym, spot_oid)
                p_ok, p_exec = self._check_order_filled(perp_sym, perp_oid)

                if s_ok and p_ok:
                    add_log(f"  [{spot_sym}] 两腿均成交 ✓")
                    return True

                if s_ok and not p_ok:
                    # 现货已成交，合约改 taker
                    add_log(f"  [{spot_sym}] 现货成交，合约改 taker")
                    self._cancel_order(perp_sym, perp_oid)
                    # 重新取卖一价（taker 用对手方最优 = 卖一）
                    _, new_ask = self._depth_bbo(spot_sym)
                    if new_ask:
                        perp_oid = self._place_perp_sell(perp_sym, qty, new_ask, post_only=False)
                        if not perp_oid:
                            # 合约腿失败，撤销现货成交？已成交无法撤，平仓处理
                            return True  # 现货已成交，部分成功
                    spot_done = perp_done = False  # 重置等待

                elif p_ok and not s_ok:
                    # 合约已成交，现货改 taker
                    add_log(f"  [{spot_sym}] 合约成交，现货改 taker")
                    self._cancel_order(spot_sym, spot_oid)
                    new_bid, _ = self._depth_bbo(spot_sym)
                    if new_bid:
                        spot_oid = self._place_spot_buy(spot_sym, qty, new_bid)
                        if not spot_oid:
                            return True  # 合约已成交，部分成功
                    spot_done = perp_done = False

            # 超时，重挂
            add_log(f"  [{spot_sym}] 超时，撤单重挂")
            self._cancel_order(spot_sym, spot_oid)
            self._cancel_order(perp_sym, perp_oid)
            time.sleep(2)
            # 刷新盘口
            bid, ask = self._depth_bbo(spot_sym)
            if not bid or not ask:
                return False

        add_log(f"  [{spot_sym}] {MAX_RETRIES}次重试均失败")
        return False

    # ---- 平仓 ----

    def _place_spot_sell(self, sym: str, qty: float, price: float) -> Optional[str]:
        action = f"现货卖出 {sym} {qty:.4f} @ {price:.4f}"
        if self.dry_run or not account:
            add_log(f"[DRY] {action}")
            return f"dry-{int(time.time()*1000)}"
        try:
            cfg = account.execute_order(
                symbol=sym, side="Ask", order_type="Limit",
                quantity=str(round(qty, 4)), price=str(round(price, 4)),
                time_in_force="GTC", self_trade_prevention="RejectTaker",
            )
            resp = account.http_client.post(cfg.url, headers=cfg.headers, data=cfg.data)
            return str(resp.get("id")) if isinstance(resp, dict) else str(resp)[:20]
        except Exception as e:
            add_log(f"[FAIL] {action}: {e}")
            return None

    def _place_perp_buy(self, sym: str, qty: float, price: float) -> Optional[str]:
        action = f"合约买入平仓 {sym} {qty:.4f} @ {price:.4f}"
        if self.dry_run or not account:
            add_log(f"[DRY] {action}")
            return f"dry-{int(time.time()*1000)}"
        try:
            cfg = account.execute_order(
                symbol=sym, side="Bid", order_type="Limit",
                quantity=str(round(qty, 4)), price=str(round(price, 4)),
                time_in_force="GTC", post_only=True,
                self_trade_prevention="RejectTaker",
            )
            resp = account.http_client.post(cfg.url, headers=cfg.headers, data=cfg.data)
            return str(resp.get("id")) if isinstance(resp, dict) else str(resp)[:20]
        except Exception as e:
            add_log(f"[FAIL] {action}: {e}")
            return None

    def close_pair(self, spot_sym: str, perp_sym: str, qty: float) -> bool:
        """平仓一对"""
        _, ask = self._depth_bbo(spot_sym)
        bid, _ = self._depth_bbo(spot_sym)
        spot_oid = self._place_spot_sell(spot_sym, qty, ask) if ask else None
        perp_oid = self._place_perp_buy(perp_sym, qty, bid) if bid else None
        return bool(spot_oid and perp_oid)


# ==================== ArbitrageEngine ====================

class ArbitrageEngine:
    """开仓/平仓编排"""

    def __init__(self):
        self.splitter = OrderSplitter()

    def open_position(self, symbol: str, notional: float, leverage: float) -> dict:
        """开仓"""
        spot_sym = f"{symbol}_USDC"
        perp_sym = f"{symbol}_USDC_PERP"

        # 计算数量
        mid = self.splitter._depth_mid(spot_sym)
        if not mid:
            return {"ok": False, "error": "无参考价"}

        total_qty = notional / mid
        pairs = max(1, int(notional / ORDER_SIZE_USDC))
        pair_notional = notional / pairs
        pair_qty = pair_notional / mid

        add_log(f"开仓 {symbol}: 名义 {notional:.0f} USDC，{pairs} 笔，每笔 {pair_notional:.0f}U / {pair_qty:.4f} {symbol}")

        # 校验杠杆
        col = pub.get_collateral()
        for c in col:
            if c["symbol"] == symbol:
                imf = float(c["imfFunction"]["base"])
                max_lev = 1 / imf if imf > 0 else 0
                if leverage > max_lev:
                    return {"ok": False, "error": f"杠杆 {leverage}x 超上限 {max_lev:.1f}x"}
                break

        ok = 0
        for i in range(pairs):
            add_log(f"[{symbol}] 第 {i+1}/{pairs} 对")
            if self.splitter.execute_pair(spot_sym, perp_sym, pair_qty):
                ok += 1
            time.sleep(2)

        if ok > 0:
            position_state[symbol] = {
                "spot_sym": spot_sym, "perp_sym": perp_sym,
                "qty": ok * pair_qty, "notional": ok * pair_notional,
                "entry_price": mid, "entry_time": datetime.now().isoformat(),
            }
        return {"ok": ok > 0, "pairs_done": ok, "pairs_total": pairs}

    def close_position(self, symbol: str) -> dict:
        """平仓"""
        pos = position_state.get(symbol)
        if not pos:
            return {"ok": False, "error": f"无 {symbol} 持仓"}
        total_qty = pos["qty"]
        pairs = max(1, int(total_qty * pos["entry_price"] / ORDER_SIZE_USDC))
        pair_qty = total_qty / pairs

        add_log(f"平仓 {symbol}: {total_qty:.4f} {symbol}")
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


# ==================== Flask API ====================

@app.route("/")
def index():
    return render_template("bpx_arb.html")


@app.route("/api/symbols")
def api_symbols():
    """可选币种列表（抵押品 ∩ 永续合约 + 实时费率）"""
    try:
        col = pub.get_collateral()
        ms = pub.get_markets()
        perp_set = {m["symbol"].replace("_USDC_PERP", "") for m in ms if m["symbol"].endswith("_PERP")}
        result = []
        for c in col:
            sym = c["symbol"]
            if sym not in perp_set:
                continue
            imf = float(c["imfFunction"]["base"])
            hc = float(c["haircutFunction"]["kind"].get("base", 0))
            max_lev = round(1 / imf, 1) if 0 < imf < 100 else 0
            # 拉最新费率
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
    """返回当前状态"""
    # 给持仓补上盈亏和费率
    enriched = {}
    for sym, pos in position_state.items():
        pos_copy = dict(pos)
        pos_copy["pnl"] = None
        pos_copy["funding_rate"] = None
        try:
            ticker = pub.get_ticker(pos["spot_sym"], source="External")
            if isinstance(ticker, dict) and ticker.get("lastPrice"):
                current = float(ticker["lastPrice"])
                pos_copy["current_price"] = current
                pos_copy["pnl"] = (current - pos["entry_price"]) * pos["qty"]
            fr = pub.get_funding_interval_rates(f"{sym}_USDC_PERP", limit=1)
            if isinstance(fr, list) and fr:
                rate = float(fr[0].get("fundingRate", 0))
                pos_copy["funding_rate"] = round(rate * 24 * 365 * 100, 1)
        except Exception:
            pass
        enriched[sym] = pos_copy

    # 维持保证金率
    maintenance_margin_ratio = None
    if account and not DRY_RUN:
        try:
            cfg = account.get_account()
            acc = account.http_client.get(cfg.url, headers=cfg.headers)
            if isinstance(acc, dict):
                # leverageLimit 可作为近似参考
                maintenance_margin_ratio = acc.get("positionLimit", acc.get("leverageLimit"))
        except Exception:
            pass

    state = {
        "dry_run": DRY_RUN,
        "has_key": bool(BPX_PUBLIC_KEY and BPX_SECRET_KEY),
        "positions": enriched,
        "active_orders": active_orders,
        "logs": operation_log[-30:],
        "maintenance_margin_ratio": maintenance_margin_ratio,
    }
    # 账户余额
    if account and not DRY_RUN:
        try:
            cfg = account.get_balances()
            balances = account.http_client.get(cfg.url, headers=cfg.headers)
            state["balances"] = balances if isinstance(balances, list) else []
        except Exception:
            state["balances"] = []
    else:
        state["balances"] = []
    return jsonify(state)


@app.route("/api/open", methods=["POST"])
def api_open():
    """开仓指令"""
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
                add_log(f"[警告] {symbol} 周均年化 {week_apy:.1f}% < {MIN_WEEK_APY}%，建议观望")
    except Exception:
        pass

    result = engine.open_position(symbol, notional, leverage)
    return jsonify(result)


@app.route("/api/close", methods=["POST"])
def api_close():
    """平仓指令"""
    data = request.get_json()
    symbol = data.get("symbol", "").strip()
    if not symbol:
        return jsonify({"ok": False, "error": "请指定币种"}), 400
    result = engine.close_position(symbol)
    return jsonify(result)


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    """撤销当前所有活跃订单"""
    data = request.get_json() or {}
    symbol = data.get("symbol", "")
    add_log(f"撤单指令: {symbol or '全部'}")
    # 通过 SDK 查开仓订单并全部取消
    if account and not DRY_RUN and symbol:
        try:
            cfg = account.cancel_all_orders(f"{symbol}_USDC")
            account.http_client.delete(cfg.url, headers=cfg.headers)
            cfg2 = account.cancel_all_orders(f"{symbol}_USDC_PERP")
            account.http_client.delete(cfg2.url, headers=cfg2.headers)
        except Exception:
            pass
    return jsonify({"ok": True})


# ---- 启动 ----
if __name__ == "__main__":
    print(f"\n{'='*50}")
    print(f"  Backpack 资金费率套利交易面板")
    print(f"  端口: {PORT} | dry_run: {DRY_RUN}")
    print(f"  浏览器打开: http://127.0.0.1:{PORT}")
    print(f"{'='*50}\n")
    app.run(host="127.0.0.1", port=PORT, debug=False)
