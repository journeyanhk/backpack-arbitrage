# -*- coding: utf-8 -*-
"""
bpx-py 美股行情补丁模块 (v0.1, 2026-08-04)

bpx-py 2.0.11 (2025-04) 未覆盖 Backpack 2026-07 新增的股票行情 API：
  - /api/v1/ticker、/api/v1/tickers、/api/v1/klines 的 source=External 参数
    （股票外部行情必须加 source=External，默认 source=Venue 非交易时段返回空）
  - /api/v1/securities、/api/v1/market-sessions、/api/v1/market-holidays

用法：
    from bpx_stock import PublicStock
    pub = PublicStock()
    ticker = pub.get_ticker("AAPL.US_USDC", source="External")
    klines = pub.get_klines("AAPL.US_USDC", "1d", start_time, source="External")
    secs = pub.get_securities()

设计：subclass Public，不动 site-packages。Backpack 后期出订单簿模式、
SDK 升级后，本模块依然兼容，届时直接用官方新版本替换即可。
"""
from typing import Optional, Union, List, Dict, Any

from bpx.public import Public
from bpx.constants.enums import TimeIntervalType, TimeIntervalEnum


class PublicStock(Public):
    """Public 扩展：支持股票外部行情 source=External 与证券列表端点。"""

    # ---------- source 参数扩展（股票外部行情） ----------

    def get_ticker_url(self, symbol: str, source: Optional[str] = None) -> str:
        url = super().get_ticker_url(symbol)
        if source:
            url += f"&source={source}"
        return url

    def get_tickers_url(self, source: Optional[str] = None) -> str:
        url = super().get_tickers_url()
        if source:
            url += f"?source={source}"
        return url

    def get_klines_url(
        self,
        symbol: str,
        interval: Union[TimeIntervalEnum, TimeIntervalType],
        start_time: int,
        end_time: Optional[int] = None,
        source: Optional[str] = None,
    ) -> str:
        url = super().get_klines_url(symbol, interval, start_time, end_time)
        if source:
            url += f"&source={source}"
        return url

    def get_ticker(self, symbol: str, source: Optional[str] = None):
        """ticker；股票行情传 source="External"，不传则保持原行为。"""
        return self.http_client.get(self.get_ticker_url(symbol, source))

    def get_tickers(self, source: Optional[str] = None):
        """全市场 ticker；股票行情传 source="External"。"""
        return self.http_client.get(self.get_tickers_url(source))

    def get_klines(
        self,
        symbol: str,
        interval: Union[TimeIntervalType, TimeIntervalEnum],
        start_time: int,
        end_time: int = 0,
        source: Optional[str] = None,
    ):
        """K 线；股票行情传 source="External"。"""
        return self.http_client.get(
            self.get_klines_url(
                symbol=symbol,
                interval=interval,
                start_time=start_time,
                end_time=end_time,
                source=source,
            )
        )

    # ---------- 证券相关端点（SDK 未覆盖，2026-07 新增） ----------

    def get_securities_url(self) -> str:
        return self._endpoint("api/v1/securities")

    def get_market_sessions_url(self) -> str:
        return self._endpoint("api/v1/market-sessions")

    def get_market_holidays_url(self) -> str:
        return self._endpoint("api/v1/market-holidays")

    def get_securities(self) -> List[Dict[str, Any]]:
        """可交易证券列表（约 1100+ 只美股/ETF，含 CUSIP、名称、各时段数量约束）。"""
        return self.http_client.get(self.get_securities_url())

    def get_market_sessions(self):
        """各市场会话的开放时间（US_EQUITIES_* 等）。"""
        return self.http_client.get(self.get_market_sessions_url())

    def get_market_holidays(self):
        """休市日与缩短交易日。"""
        return self.http_client.get(self.get_market_holidays_url())

    def get_stock_markets(self) -> List[Dict[str, Any]]:
        """仅返回现货订单簿股票市场（rwaMarketType=STOCK）。"""
        ms = self.get_markets()
        if isinstance(ms, list):
            return [m for m in ms if m.get("rwaMarketType") == "STOCK"]
        return []
