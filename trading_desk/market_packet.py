"""Fast, auditable market-data packets for the trading-desk Agent.

The module deliberately fetches only the current holdings and candidate pool.
It is not a market screener and it never changes account, candidate-pool, or
order state. Its job is to provide a bounded, repeatable data packet before
an Agent starts reasoning.
"""

from __future__ import annotations

import json
import math
import os
import re
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

try:
    import requests
except ImportError:  # pragma: no cover - urllib remains the dependency-free fallback
    requests = None

from . import sector_data, state


# China Standard Time has no daylight saving transition.  Using a fixed offset
# avoids requiring the optional ``tzdata`` wheel on Windows installations.
SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
USER_AGENT = "StockPet-inspired A-share Trading Desk/0.2"
LATEST_TIMEOUT_SECONDS = 2.5
SECONDARY_TIMEOUT_SECONDS = 4.5
INTRADAY_TIMEOUT_SECONDS = 4.5
HISTORY_TIMEOUT_SECONDS = 4.5
FRESHNESS_SECONDS = 90
MAX_PARALLEL_REQUESTS = 6
PYTHON_HTTP_RETRIES = 1
_HTTP_LOCAL = threading.local()
MARKET_BENCHMARKS = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sh000300": "沪深300",
}
NODE_NAMES = {
    "09:08": "盘前准备", "09:22": "开盘计划", "10:30": "上午复核", "11:25": "午间复盘",
    "13:00": "午后开盘", "14:25": "尾盘确认", "14:50": "收盘风险", "15:05": "收盘复盘",
}
ANALYSIS_TRIGGERS = {
    "startup": "当日首次打开",
    "timer": "一小时计时到期",
    "manual": "用户主动要求",
    "monitor": "监控信号触发",
    "market_close": "收盘复盘",
}


class MarketDataError(RuntimeError):
    """A source failed, but packet generation may still continue from cache."""


def shanghai_now() -> datetime:
    return datetime.now(SHANGHAI)


def intraday_expectation(value: datetime | None = None) -> dict[str, Any]:
    current = value or shanghai_now()
    current = current.replace(tzinfo=SHANGHAI) if current.tzinfo is None else current.astimezone(SHANGHAI)
    if current.strftime("%H:%M") < "09:30":
        return {
            "expected": False,
            "status": "not_expected",
            "reason": "连续竞价尚未开始；腾讯分时通常不足两个有效分钟点，盘前分析不请求分时。",
        }
    return {"expected": True, "status": "expected", "reason": "连续竞价已经开始，应请求腾讯分时。"}


def normalize_tencent_code(code: str) -> str:
    normalized = str(code).strip().lower()
    if normalized.startswith(("sh", "sz")):
        return normalized
    if not re.fullmatch(r"\d{6}", normalized):
        raise ValueError(f"不支持的A股代码：{code}")
    return ("sh" if normalized.startswith("6") else "sz") + normalized


def eastmoney_secid(code: str) -> str:
    raw = str(code).lower().removeprefix("sh").removeprefix("sz")
    return ("1." if raw.startswith("6") else "0.") + raw


def _http_bytes(url: str, timeout: float) -> bytes:
    """Fetch bytes in-process; never create curl.exe or another console child."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Connection": "close",
    }
    if requests is not None:
        session = getattr(_HTTP_LOCAL, "session", None)
        if session is None:
            session = requests.Session()
            # Direct Tencent access is materially faster than Windows' ambient
            # proxy discovery in this workspace.  No credentials are involved.
            session.trust_env = False
            _HTTP_LOCAL.session = session
        last_error: Exception | None = None
        for attempt in range(PYTHON_HTTP_RETRIES + 1):
            try:
                response = session.get(
                    url,
                    headers=headers,
                    timeout=(max(1.0, timeout), max(2.0, timeout + 1.0)),
                    allow_redirects=True,
                )
                response.raise_for_status()
                if not response.content:
                    raise MarketDataError("Python HTTP response was empty")
                return bytes(response.content)
            except Exception as exc:
                last_error = exc
                if attempt < PYTHON_HTTP_RETRIES:
                    time.sleep(0.1 * (attempt + 1))
        assert last_error is not None
        if isinstance(last_error, MarketDataError):
            raise last_error
        detail = str(last_error).strip() or type(last_error).__name__
        raise MarketDataError(f"Python HTTP {type(last_error).__name__}: {detail[:180]}") from last_error

    request = Request(url, headers=headers)
    context = ssl.create_default_context()
    if hasattr(ssl, "OP_IGNORE_UNEXPECTED_EOF"):
        context.options |= ssl.OP_IGNORE_UNEXPECTED_EOF
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context))
    try:
        with opener.open(request, timeout=timeout) as response:  # nosec B310: fixed public HTTPS endpoints
            payload = response.read()
            if not payload:
                raise MarketDataError("Python HTTP response was empty")
            return payload
    except Exception as exc:
        if isinstance(exc, MarketDataError):
            raise
        detail = str(exc).strip() or type(exc).__name__
        raise MarketDataError(f"Python HTTP {type(exc).__name__}: {detail[:180]}") from exc


def _parse_timestamp(raw: str | None) -> str | None:
    value = str(raw or "").strip()
    if not re.fullmatch(r"\d{14}", value):
        return None
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI).isoformat()
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _non_negative_number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _round(value: float | None, digits: int = 4) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def _pct_change(current: float | None, base: float | None) -> float | None:
    if current is None or base is None or base <= 0:
        return None
    return _round((current / base - 1) * 100)


def _market_elapsed_minutes(now: datetime) -> int:
    minutes = now.hour * 60 + now.minute
    morning_open, morning_close = 9 * 60 + 30, 11 * 60 + 30
    afternoon_open, afternoon_close = 13 * 60, 15 * 60
    if minutes <= morning_open:
        return 0
    if minutes <= morning_close:
        return minutes - morning_open
    if minutes < afternoon_open:
        return 120
    if minutes <= afternoon_close:
        return 120 + minutes - afternoon_open
    return 240


def _sample_series(points: list[dict[str, Any]], limit: int = 24) -> list[dict[str, Any]]:
    if len(points) <= limit:
        selected = points
    else:
        indices = sorted({round(index * (len(points) - 1) / (limit - 1)) for index in range(limit)})
        selected = [points[index] for index in indices]
    return [{"time": item["time"], "price": item["price"]} for item in selected]


def _return_over_points(points: list[dict[str, Any]], lookback: int) -> float | None:
    if len(points) < 2:
        return None
    earlier = points[max(0, len(points) - lookback - 1)]["price"]
    return _pct_change(points[-1]["price"], earlier)


def _linear_slope_pct(points: list[dict[str, Any]], lookback: int = 30) -> float | None:
    sample = points[-lookback:]
    if len(sample) < 3:
        return None
    prices = [float(item["price"]) for item in sample]
    x_mean = (len(prices) - 1) / 2
    y_mean = sum(prices) / len(prices)
    denominator = sum((index - x_mean) ** 2 for index in range(len(prices)))
    if denominator <= 0 or y_mean <= 0:
        return None
    slope = sum((index - x_mean) * (price - y_mean) for index, price in enumerate(prices)) / denominator
    return _round(slope * 30 / y_mean * 100)


def _max_drawdown_pct(points: list[dict[str, Any]]) -> float | None:
    if not points:
        return None
    peak = float(points[0]["price"])
    drawdown = 0.0
    for item in points:
        price = float(item["price"])
        peak = max(peak, price)
        if peak > 0:
            drawdown = min(drawdown, (price / peak - 1) * 100)
    return _round(drawdown)


def _intraday_features(points: list[dict[str, Any]], quote: dict[str, Any] | None = None) -> dict[str, Any]:
    if len(points) < 2:
        return {}
    prices = [float(item["price"]) for item in points]
    first_price, last_price = prices[0], prices[-1]
    high_price, low_price = max(prices), min(prices)
    high_index, low_index = prices.index(high_price), prices.index(low_price)
    path_length = sum(abs(right - left) for left, right in zip(prices, prices[1:]))
    efficiency = abs(last_price - first_price) / path_length if path_length > 0 else 0.0
    range_position = (last_price - low_price) / (high_price - low_price) if high_price > low_price else 0.5
    cumulative_volume = _non_negative_number(points[-1].get("cumulative_volume_lots"))
    cumulative_amount = _non_negative_number(points[-1].get("cumulative_amount_cny"))
    vwap_price = None
    if cumulative_volume and cumulative_amount:
        vwap_price = cumulative_amount / (cumulative_volume * 100)
    if vwap_price is None and quote:
        vwap_price = _number(quote.get("vwap_price"))
    recent_volume_acceleration = None
    if len(points) >= 11:
        end_volume = _non_negative_number(points[-1].get("cumulative_volume_lots"))
        five_ago = _non_negative_number(points[-6].get("cumulative_volume_lots"))
        ten_ago = _non_negative_number(points[-11].get("cumulative_volume_lots"))
        if end_volume is not None and five_ago is not None and ten_ago is not None:
            latest_five = max(end_volume - five_ago, 0)
            prior_five = max(five_ago - ten_ago, 0)
            recent_volume_acceleration = _round(latest_five / prior_five, 3) if prior_five > 0 else None
    open_to_now = _pct_change(last_price, first_price)
    pullback_from_high = _pct_change(last_price, high_price)
    rebound_from_low = _pct_change(last_price, low_price)
    price_vs_vwap = _pct_change(last_price, vwap_price)
    slope = _linear_slope_pct(points)
    if pullback_from_high is not None and pullback_from_high <= -1.5 and high_index < len(points) * 0.65:
        structure = "冲高回落"
    elif rebound_from_low is not None and rebound_from_low >= 1.5 and low_index < len(points) * 0.65:
        structure = "探底回升"
    elif slope is not None and slope >= 0.5 and efficiency >= 0.35 and (price_vs_vwap or 0) >= 0:
        structure = "稳步走强"
    elif slope is not None and slope <= -0.5 and efficiency >= 0.35 and (price_vs_vwap or 0) <= 0:
        structure = "持续走弱"
    elif range_position >= 0.7:
        structure = "区间偏强"
    elif range_position <= 0.3:
        structure = "区间偏弱"
    else:
        structure = "震荡"
    return {
        "open_to_now_pct": open_to_now,
        "last_5m_return_pct": _return_over_points(points, 5),
        "last_15m_return_pct": _return_over_points(points, 15),
        "last_30m_return_pct": _return_over_points(points, 30),
        "high_price": _round(high_price),
        "high_time": points[high_index]["time"],
        "low_price": _round(low_price),
        "low_time": points[low_index]["time"],
        "pullback_from_high_pct": pullback_from_high,
        "rebound_from_low_pct": rebound_from_low,
        "range_position": _round(range_position, 3),
        "vwap_price": _round(vwap_price),
        "price_vs_vwap_pct": price_vs_vwap,
        "trend_slope_pct_per_30m": slope,
        "trend_efficiency": _round(efficiency, 3),
        "max_drawdown_pct": _max_drawdown_pct(points),
        "recent_5m_volume_acceleration": recent_volume_acceleration,
        "structure": structure,
    }


def parse_tencent_kline(payload: dict[str, Any], code: str) -> list[dict[str, Any]]:
    key = normalize_tencent_code(code)
    block = ((payload.get("data") or {}).get(key) or {})
    rows = block.get("qfqday") or block.get("day") or []
    bars = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        try:
            bars.append({
                "date": str(row[0]),
                "open": float(row[1]),
                "close": float(row[2]),
                "high": float(row[3]),
                "low": float(row[4]),
                "volume_lots": float(row[5]),
            })
        except (TypeError, ValueError):
            continue
    return sorted(bars, key=lambda item: item["date"])


def _technical_features(
    bars: list[dict[str, Any]],
    quote: dict[str, Any],
    now: datetime,
    opened_on: str | None = None,
) -> dict[str, Any]:
    if not bars or not quote.get("last_price"):
        return {}
    today = now.date().isoformat()
    complete = [dict(item) for item in bars if item.get("date") != today]
    current_bar = {
        "date": today,
        "open": quote.get("open_price") or quote["last_price"],
        "close": quote["last_price"],
        "high": quote.get("high_price") or quote["last_price"],
        "low": quote.get("low_price") or quote["last_price"],
        "volume_lots": quote.get("volume_lots") or quote.get("volume"),
    }
    combined = complete + [current_bar]
    closes = [float(item["close"]) for item in combined if _number(item.get("close"))]
    if not closes:
        return {}

    def moving_average(window: int) -> float | None:
        if len(closes) < window:
            return None
        return _round(sum(closes[-window:]) / window)

    def period_return(window: int) -> float | None:
        if len(closes) <= window:
            return None
        return _pct_change(closes[-1], closes[-window - 1])

    true_ranges = []
    for index in range(1, len(combined)):
        current = combined[index]
        previous_close = float(combined[index - 1]["close"])
        true_ranges.append(max(
            float(current["high"]) - float(current["low"]),
            abs(float(current["high"]) - previous_close),
            abs(float(current["low"]) - previous_close),
        ))
    atr14 = sum(true_ranges[-14:]) / min(14, len(true_ranges)) if true_ranges else None
    prior_volumes = [float(item["volume_lots"]) for item in complete if _number(item.get("volume_lots"))]
    avg_volume_5 = sum(prior_volumes[-5:]) / min(5, len(prior_volumes)) if prior_volumes else None
    avg_volume_20 = sum(prior_volumes[-20:]) / min(20, len(prior_volumes)) if prior_volumes else None
    elapsed_minutes = _market_elapsed_minutes(now)
    current_volume = _number(current_bar.get("volume_lots"))
    volume_ratio = None
    if current_volume and avg_volume_5 and elapsed_minutes > 0:
        volume_ratio = current_volume / elapsed_minutes / (avg_volume_5 / 240)
    recent_20 = combined[-20:]
    high_20 = max(float(item["high"]) for item in recent_20)
    low_20 = min(float(item["low"]) for item in recent_20)
    ma5, ma10, ma20, ma60 = (moving_average(window) for window in (5, 10, 20, 60))
    if ma5 and ma10 and ma20 and ma5 > ma10 > ma20:
        alignment = "多头排列"
    elif ma5 and ma10 and ma20 and ma5 < ma10 < ma20:
        alignment = "空头排列"
    else:
        alignment = "均线交错"
    highest_close_since_opened = None
    if opened_on:
        holding_closes = [float(item["close"]) for item in combined if str(item.get("date")) >= opened_on]
        highest_close_since_opened = max(holding_closes) if holding_closes else None
    return {
        "history_bars": len(bars),
        "history_as_of": bars[-1].get("date"),
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "ma60": ma60,
        "price_vs_ma20_ratio": _round(closes[-1] / ma20, 4) if ma20 else None,
        "ma_alignment": alignment,
        "return_5d_pct": period_return(5),
        "return_10d_pct": period_return(10),
        "return_20d_pct": period_return(20),
        "atr14": _round(atr14),
        "atr14_pct": _pct_change(closes[-1] + atr14, closes[-1]) if atr14 else None,
        "high_20d": _round(high_20),
        "low_20d": _round(low_20),
        "distance_from_20d_high_pct": _pct_change(closes[-1], high_20),
        "distance_from_20d_low_pct": _pct_change(closes[-1], low_20),
        "avg_volume_5d_lots": _round(avg_volume_5, 1),
        "avg_volume_20d_lots": _round(avg_volume_20, 1),
        "elapsed_trading_minutes": elapsed_minutes,
        "estimated_volume_ratio_5d": _round(volume_ratio, 3),
        "highest_close_since_opened": _round(highest_close_since_opened),
    }


def _quote_age_seconds(timestamp: str | None, now: datetime) -> int | None:
    if not timestamp:
        return None
    try:
        value = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    return max(0, int((now - value).total_seconds()))


def parse_tencent_realtime(raw: bytes, codes: list[str]) -> dict[str, dict[str, Any]]:
    text = raw.decode("gb18030", errors="replace")
    requested = {normalize_tencent_code(code): str(code).removeprefix("sh").removeprefix("sz") for code in codes}
    quotes: dict[str, dict[str, Any]] = {}
    for key, body in re.findall(r'v_([^=]+)="?(.*?)"?;', text, flags=re.DOTALL):
        code = requested.get(key.lower())
        if not code:
            continue
        fields = body.split("~")
        if len(fields) <= 30:
            continue
        last_price, previous_close = _number(fields[3]), _number(fields[4])
        if not last_price or not previous_close:
            continue
        volume_lots = _non_negative_number(fields[6])
        amount_wan = _non_negative_number(fields[37]) if len(fields) > 37 else None
        bid1_price = _number(fields[9]) if len(fields) > 9 else None
        ask1_price = _number(fields[19]) if len(fields) > 19 else None
        quotes[code] = {
            "code": code,
            "name": fields[1].strip(),
            "last_price": last_price,
            "previous_close": previous_close,
            "open_price": _number(fields[5]),
            "high_price": _number(fields[33]) if len(fields) > 33 else None,
            "low_price": _number(fields[34]) if len(fields) > 34 else None,
            "volume": volume_lots,
            "volume_lots": volume_lots,
            "volume_shares": _round(volume_lots * 100, 0) if volume_lots is not None else None,
            "amount": _round(amount_wan * 10_000, 2) if amount_wan is not None else None,
            "amount_cny": _round(amount_wan * 10_000, 2) if amount_wan is not None else None,
            "bid1_price": bid1_price,
            "bid1_volume_lots": _non_negative_number(fields[10]) if len(fields) > 10 else None,
            "ask1_price": ask1_price,
            "ask1_volume_lots": _non_negative_number(fields[20]) if len(fields) > 20 else None,
            "turnover_rate_pct": _non_negative_number(fields[38]) if len(fields) > 38 else None,
            "quote_timestamp": _parse_timestamp(fields[30]),
            "source": "腾讯实时行情",
        }
    return quotes


def parse_eastmoney_realtime(payload: dict[str, Any], codes: list[str]) -> dict[str, dict[str, Any]]:
    requested = {str(code).removeprefix("sh").removeprefix("sz") for code in codes}
    quotes: dict[str, dict[str, Any]] = {}
    for item in ((payload.get("data") or {}).get("diff") or []):
        code = str(item.get("f12") or "")
        if code not in requested:
            continue
        last_price, previous_close = _number(item.get("f2")), _number(item.get("f18"))
        if not last_price or not previous_close:
            continue
        timestamp = None
        try:
            timestamp = datetime.fromtimestamp(int(item.get("f124")), tz=timezone.utc).astimezone(SHANGHAI).isoformat()
        except (TypeError, ValueError, OSError, OverflowError):
            pass
        volume_lots = _non_negative_number(item.get("f5"))
        amount_cny = _non_negative_number(item.get("f6"))
        quotes[code] = {
            "code": code,
            "name": str(item.get("f14") or ""),
            "last_price": last_price,
            "previous_close": previous_close,
            "open_price": _number(item.get("f17")),
            "high_price": _number(item.get("f15")),
            "low_price": _number(item.get("f16")),
            "volume": volume_lots,
            "volume_lots": volume_lots,
            "volume_shares": _round(volume_lots * 100, 0) if volume_lots is not None else None,
            "amount": amount_cny,
            "amount_cny": amount_cny,
            "quote_timestamp": timestamp,
            "source": "东方财富实时行情",
        }
    return quotes


def parse_tencent_intraday(
    payload: dict[str, Any],
    code: str,
    quote: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    key = normalize_tencent_code(code)
    block = ((payload.get("data") or {}).get(key) or {})
    rows = ((block.get("data") or {}).get("data") or [])
    date = str((block.get("data") or {}).get("date") or "")
    points = []
    for row in rows:
        parts = str(row).split()
        price = _number(parts[1]) if len(parts) > 1 else None
        if len(parts) > 1 and re.fullmatch(r"\d{4}", parts[0]) and price:
            points.append({
                "time": f"{date} {parts[0]}",
                "price": price,
                "cumulative_volume_lots": _non_negative_number(parts[2]) if len(parts) > 2 else None,
                "cumulative_amount_cny": _non_negative_number(parts[3]) if len(parts) > 3 else None,
            })
    if len(points) < 2:
        return None
    return {
        "source": "腾讯分时",
        "points_count": len(points),
        "latest_time": points[-1]["time"],
        "latest_price": points[-1]["price"],
        "sampled_path": _sample_series(points),
        "features": _intraday_features(points, quote),
    }


def parse_eastmoney_intraday(
    payload: dict[str, Any],
    quote: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    rows = ((payload.get("data") or {}).get("trends") or [])
    points = []
    for row in rows:
        parts = str(row).split(",")
        price = _number(parts[2]) if len(parts) > 2 else None
        if len(parts) > 2 and price:
            points.append({
                "time": parts[0],
                "price": price,
                "cumulative_volume_lots": _non_negative_number(parts[5]) if len(parts) > 5 else None,
                "cumulative_amount_cny": _non_negative_number(parts[6]) if len(parts) > 6 else None,
            })
    if len(points) < 2:
        return None
    return {
        "source": "东方财富分时备用",
        "points_count": len(points),
        "latest_time": points[-1]["time"],
        "latest_price": points[-1]["price"],
        "sampled_path": _sample_series(points),
        "features": _intraday_features(points, quote),
    }


class MarketPacketBuilder:
    def __init__(
        self,
        fetcher: Callable[[str, float], bytes] = _http_bytes,
        cache_path: Path | None = None,
        sector_client: Any | None = None,
    ) -> None:
        self.fetcher = fetcher
        self.cache_path = cache_path or state.STATE_DIR / "market_cache.json"
        self.sector_client = sector_client or sector_data.TencentSectorClient()

    def _fetch_tencent_latest(self, codes: list[str]) -> dict[str, dict[str, Any]]:
        request_codes = ",".join(normalize_tencent_code(code) for code in codes)
        return parse_tencent_realtime(self.fetcher(f"https://qt.gtimg.cn/q={request_codes}", LATEST_TIMEOUT_SECONDS), codes)

    def _fetch_eastmoney_latest(self, codes: list[str]) -> dict[str, dict[str, Any]]:
        query = urlencode({
            "fltt": "2", "secids": ",".join(eastmoney_secid(code) for code in codes),
            "fields": "f12,f14,f2,f3,f4,f5,f6,f15,f16,f17,f18,f124",
        })
        raw = self.fetcher(f"https://push2.eastmoney.com/api/qt/ulist.np/get?{query}", SECONDARY_TIMEOUT_SECONDS)
        return parse_eastmoney_realtime(json.loads(raw.decode("utf-8")), codes)

    def _fetch_tencent_kline(self, code: str) -> list[dict[str, Any]]:
        tencent_code = normalize_tencent_code(code)
        raw = self.fetcher(
            f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={tencent_code},day,,,120,qfq",
            HISTORY_TIMEOUT_SECONDS,
        )
        return parse_tencent_kline(json.loads(raw.decode("utf-8")), code)

    def _fetch_intraday(self, code: str, quote: dict[str, Any] | None = None) -> dict[str, Any]:
        tencent_code = normalize_tencent_code(code)
        try:
            raw = self.fetcher(f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={tencent_code}", INTRADAY_TIMEOUT_SECONDS)
            result = parse_tencent_intraday(json.loads(raw.decode("utf-8")), code, quote)
            if result:
                return {"ok": True, **result}
            raise MarketDataError("腾讯分时没有足够绘图点")
        except Exception as primary_error:
            detail = str(primary_error).strip() or type(primary_error).__name__
            return {
                "ok": False,
                "status": "unavailable",
                "source": "腾讯分时",
                "tradeable": False,
                "error_type": type(primary_error).__name__,
                "error_detail": detail[:240],
                "policy": "腾讯失败即标记不可用；盘中禁止自动切换非腾讯数据源。",
            }

    def _load_cache(self) -> dict[str, Any]:
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {"quotes": {}, "intraday": {}, "klines": {}, "market": {}}

    def _save_cache(
        self,
        quotes: dict[str, Any],
        intraday: dict[str, Any],
        klines: dict[str, Any],
        market: dict[str, Any],
    ) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "updated_at": shanghai_now().isoformat(),
            "quotes": quotes,
            "intraday": intraday,
            "klines": klines,
            "market": market,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.cache_path)

    @staticmethod
    def _combined_quote(code: str, primary: dict[str, Any] | None, secondary: dict[str, Any] | None, cached: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
        def compliant(quote: dict[str, Any] | None) -> bool:
            if not quote:
                return False
            age_seconds = _quote_age_seconds(quote.get("quote_timestamp"), now)
            return (
                _number(quote.get("last_price")) is not None
                and _number(quote.get("previous_close")) is not None
                and age_seconds is not None
                and age_seconds <= FRESHNESS_SECONDS
            )

        selected = primary if compliant(primary) else secondary if compliant(secondary) else primary or secondary or cached
        if not selected:
            return {"code": code, "status": "unavailable", "tradeable": False, "is_stale": True}
        result = dict(selected)
        result["change_pct"] = _pct_change(result["last_price"], result["previous_close"])
        result["gap_pct"] = _pct_change(result.get("open_price"), result.get("previous_close"))
        high_price, low_price = _number(result.get("high_price")), _number(result.get("low_price"))
        result["amplitude_pct"] = _round((high_price - low_price) / result["previous_close"] * 100) if high_price and low_price else None
        result["range_position"] = _round((result["last_price"] - low_price) / (high_price - low_price), 3) if high_price and low_price and high_price > low_price else None
        amount_cny = _number(result.get("amount_cny") or result.get("amount"))
        volume_lots = _number(result.get("volume_lots") or result.get("volume"))
        result["vwap_price"] = _round(amount_cny / (volume_lots * 100)) if amount_cny and volume_lots else None
        result["price_vs_vwap_pct"] = _pct_change(result["last_price"], result.get("vwap_price"))
        bid1_price, ask1_price = _number(result.get("bid1_price")), _number(result.get("ask1_price"))
        result["bid_ask_spread_pct"] = _round((ask1_price - bid1_price) / result["last_price"] * 100) if bid1_price and ask1_price and ask1_price >= bid1_price else None
        result["primary"], result["secondary"] = primary, secondary
        result["age_seconds"] = _quote_age_seconds(result.get("quote_timestamp"), now)
        result["is_stale"] = (primary is None and secondary is None) or not compliant(selected)
        result["cross_source_difference_pct"] = None
        if selected is primary and compliant(primary):
            result["status"] = "primary_ready"
        elif selected is secondary and compliant(secondary):
            result["status"] = "fallback_ready"
        elif primary is None and secondary is None:
            result["status"] = "cached"
        else:
            result["status"] = "stale_or_invalid"
        result["tradeable"] = result["status"] in {"primary_ready", "fallback_ready"}
        return result


    def _legacy_build_v2(self, node: str, include_intraday: bool = True, persist: bool = True) -> dict[str, Any]:
        """Build the version-2 decision packet without mutating trading state.

        Quotes are fetched in one Tencent batch.  Intraday paths and missing
        daily histories are fetched concurrently; history is cached once per
        Shanghai calendar day.  Secondary quotes are requested only for
        symbols whose Tencent quote is absent or stale.
        """
        raise state.DeskError(
            "固定节点 v2 数据包已停用；请使用 build(trigger=...) 的动态腾讯数据包。"
        )
        # Retained below only as a read-only migration reference for old archives.
        if node not in NODE_NAMES:
            raise ValueError(f"未知节点：{node}；可用节点为 {', '.join(NODE_NAMES)}")
        started = time.perf_counter()
        now = shanghai_now()
        today = now.date().isoformat()
        account, watchlist = state.get_account(), state.get_watchlist()
        positions, candidates = list(account.get("positions", [])), list(watchlist.get("candidates", []))
        position_by_code = {str(item["code"]): item for item in positions}
        candidate_by_code = {str(item["code"]): item for item in candidates}
        instruments: dict[str, dict[str, Any]] = {}
        for position in positions:
            instruments[str(position["code"])] = {"role": "holding", "name": position["name"]}
        for candidate in candidates:
            instruments.setdefault(str(candidate["code"]), {"role": "candidate", "name": candidate["name"]})
        codes = list(instruments)
        benchmark_codes = list(MARKET_BENCHMARKS)
        benchmark_raw_codes = [code[2:] for code in benchmark_codes]
        cache = self._load_cache()
        source_health: list[dict[str, Any]] = []

        latest: dict[str, dict[str, dict[str, Any]]] = {"腾讯实时行情": {}, "东方财富实时行情": {}}
        quote_started = time.perf_counter()
        try:
            batch = self._fetch_tencent_latest(codes + benchmark_codes)
            latest["腾讯实时行情"] = {code: batch[code] for code in codes if code in batch}
            benchmark_latest = {code: batch[code] for code in benchmark_raw_codes if code in batch}
            source_health.append({
                "name": "腾讯实时行情",
                "status": "online",
                "symbols": len(batch),
                "elapsed_ms": round((time.perf_counter() - quote_started) * 1000),
                "policy": "主源合规即停止，不进行双源核验",
            })
        except Exception as exc:
            benchmark_latest = {}
            source_health.append({
                "name": "腾讯实时行情",
                "status": "offline",
                "elapsed_ms": round((time.perf_counter() - quote_started) * 1000),
                "error": str(exc)[:160],
            })

        fallback_codes = []
        for code in codes:
            quote = latest["腾讯实时行情"].get(code)
            age_seconds = _quote_age_seconds((quote or {}).get("quote_timestamp"), now)
            if not quote or age_seconds is None or age_seconds > FRESHNESS_SECONDS:
                fallback_codes.append(code)
        if fallback_codes:
            fallback_started = time.perf_counter()
            try:
                latest["东方财富实时行情"] = self._fetch_eastmoney_latest(fallback_codes)
                source_health.append({
                    "name": "东方财富实时行情",
                    "status": "online",
                    "symbols": len(latest["东方财富实时行情"]),
                    "requested_symbols": len(fallback_codes),
                    "elapsed_ms": round((time.perf_counter() - fallback_started) * 1000),
                    "reason": "仅为腾讯缺失或过期标的兜底",
                })
            except Exception as exc:
                source_health.append({
                    "name": "东方财富实时行情",
                    "status": "offline",
                    "requested_symbols": len(fallback_codes),
                    "elapsed_ms": round((time.perf_counter() - fallback_started) * 1000),
                    "error": str(exc)[:160],
                })
        else:
            source_health.append({
                "name": "东方财富实时行情",
                "status": "not_called",
                "symbols": 0,
                "elapsed_ms": 0,
                "reason": "腾讯主源已返回全部合规报价",
            })

        quotes = {
            code: self._combined_quote(
                code,
                latest["腾讯实时行情"].get(code),
                latest["东方财富实时行情"].get(code),
                (cache.get("quotes") or {}).get(code),
                now,
            )
            for code in codes
        }
        market_cache = dict(cache.get("market") or {})
        benchmark_quotes = {
            raw_code: self._combined_quote(
                raw_code,
                benchmark_latest.get(raw_code),
                None,
                market_cache.get(raw_code),
                now,
            )
            for raw_code in benchmark_raw_codes
        }

        kline_cache = dict(cache.get("klines") or {})
        history_codes = list(dict.fromkeys(codes + benchmark_codes))
        history_bars: dict[str, list[dict[str, Any]]] = {}
        missing_history: list[str] = []
        for code in history_codes:
            raw_code = code[2:] if code.startswith(("sh", "sz")) else code
            cached_entry = kline_cache.get(raw_code) or {}
            if cached_entry.get("fetched_on") == today and isinstance(cached_entry.get("bars"), list):
                history_bars[raw_code] = cached_entry["bars"]
            else:
                missing_history.append(code)
        history_started = time.perf_counter()
        history_errors: list[str] = []
        retry_history: list[str] = []
        if missing_history:
            with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_REQUESTS, len(missing_history))) as executor:
                futures = {executor.submit(self._fetch_tencent_kline, code): code for code in missing_history}
                for future in as_completed(futures):
                    requested_code = futures[future]
                    raw_code = requested_code[2:] if requested_code.startswith(("sh", "sz")) else requested_code
                    try:
                        bars = future.result()
                        if not bars:
                            raise MarketDataError("腾讯日K未返回有效记录")
                        history_bars[raw_code] = bars
                        kline_cache[raw_code] = {"fetched_on": today, "bars": bars}
                    except Exception as exc:
                        cached_entry = kline_cache.get(raw_code) or {}
                        if isinstance(cached_entry.get("bars"), list):
                            history_bars[raw_code] = cached_entry["bars"]
                        retry_history.append(requested_code)
                        history_errors.append(f"{raw_code}:{type(exc).__name__}")
        retry_recovered = 0
        if retry_history:
            # One bounded retry repairs the common VPN cold-DNS case: the first
            # parallel batch warms the resolver and the immediate retry is
            # typically sub-second.  A second failure is not retried again.
            with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_REQUESTS, len(retry_history))) as executor:
                futures = {executor.submit(self._fetch_tencent_kline, code): code for code in retry_history}
                for future in as_completed(futures):
                    requested_code = futures[future]
                    raw_code = requested_code[2:] if requested_code.startswith(("sh", "sz")) else requested_code
                    try:
                        bars = future.result()
                        if not bars:
                            raise MarketDataError("腾讯日K重试仍无有效记录")
                        history_bars[raw_code] = bars
                        kline_cache[raw_code] = {"fetched_on": today, "bars": bars}
                        retry_recovered += 1
                    except Exception as exc:
                        history_errors.append(f"{raw_code}:retry:{type(exc).__name__}")
        stale_history_codes = [
            (code[2:] if code.startswith(("sh", "sz")) else code)
            for code in history_codes
            if (kline_cache.get(code[2:] if code.startswith(("sh", "sz")) else code) or {}).get("fetched_on") != today
            and (code[2:] if code.startswith(("sh", "sz")) else code) in history_bars
        ]
        source_health.append({
            "name": "腾讯前复权日K",
            "status": "online" if history_bars and not stale_history_codes else "degraded" if history_bars else "offline",
            "symbols": len(history_bars),
            "network_requests": len(missing_history),
            "cache_hits": len(history_codes) - len(missing_history),
            "retry_requests": len(retry_history),
            "retry_recovered": retry_recovered,
            "stale_cache_symbols": stale_history_codes,
            "elapsed_ms": round((time.perf_counter() - history_started) * 1000),
            "attempt_errors": history_errors[:8],
            "unrecovered_symbols": [
                (code[2:] if code.startswith(("sh", "sz")) else code)
                for code in history_codes
                if (code[2:] if code.startswith(("sh", "sz")) else code) not in history_bars
            ],
        })

        intraday: dict[str, Any] = {}
        intraday_started = time.perf_counter()
        if include_intraday and codes:
            with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_REQUESTS, len(codes))) as executor:
                futures = {executor.submit(self._fetch_intraday, code, quotes.get(code)): code for code in codes}
                for future in as_completed(futures):
                    code = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = {"ok": False, "source": "分时行情", "error": type(exc).__name__}
                    if not result.get("ok") and (cache.get("intraday") or {}).get(code):
                        result = {
                            **(cache.get("intraday") or {})[code],
                            "ok": False,
                            "cached": True,
                            "error": result.get("error"),
                        }
                    intraday[code] = result
            successful = sum(bool(value.get("ok")) for value in intraday.values())
            source_health.append({
                "name": "分时行情",
                "status": "online" if successful else "offline",
                "symbols": successful,
                "elapsed_ms": round((time.perf_counter() - intraday_started) * 1000),
            })
        else:
            source_health.append({"name": "分时行情", "status": "not_requested", "symbols": 0, "elapsed_ms": 0})

        benchmark_rows = []
        for full_code, name in MARKET_BENCHMARKS.items():
            raw_code = full_code[2:]
            quote = benchmark_quotes[raw_code]
            technical = _technical_features(history_bars.get(raw_code, []), quote, now)
            benchmark_rows.append({
                "code": raw_code,
                "name": name,
                "quote": {key: quote.get(key) for key in (
                    "last_price", "previous_close", "change_pct", "open_price", "high_price",
                    "low_price", "quote_timestamp", "source", "status", "tradeable",
                )},
                "technical": {key: technical.get(key) for key in (
                    "return_5d_pct", "return_20d_pct", "ma_alignment", "price_vs_ma20_ratio",
                )},
            })
        live_changes = [item["quote"].get("change_pct") for item in benchmark_rows if item["quote"].get("tradeable")]
        positive_count = sum(change > 0 for change in live_changes if change is not None)
        if len(live_changes) < 2:
            market_regime = "数据不足"
        elif positive_count == len(live_changes):
            market_regime = "指数普涨"
        elif positive_count >= 2:
            market_regime = "指数偏强但有分化"
        elif positive_count == 1:
            market_regime = "指数偏弱且有分化"
        else:
            market_regime = "指数普跌"
        market_context = {
            "regime": market_regime,
            "positive_benchmarks": positive_count,
            "average_change_pct": _round(sum(live_changes) / len(live_changes)) if live_changes else None,
            "benchmarks": benchmark_rows,
            "scope_note": "指数代理用于判断大盘环境；未获取全市场涨跌家数或板块资金时不得臆造市场广度。",
        }
        benchmark_csi = next((item for item in benchmark_rows if item["code"] == "000300"), None) or {}
        benchmark_csi_change = (benchmark_csi.get("quote") or {}).get("change_pct")
        benchmark_csi_5d = (benchmark_csi.get("technical") or {}).get("return_5d_pct")

        rows = []
        for code, identity in instruments.items():
            quote = quotes[code]
            opened_on = position_by_code.get(code, {}).get("opened_on")
            technical = _technical_features(history_bars.get(code, []), quote, now, opened_on)
            relative_strength = {
                "vs_csi300_intraday_pct_points": _round(quote.get("change_pct") - benchmark_csi_change) if quote.get("change_pct") is not None and benchmark_csi_change is not None else None,
                "vs_csi300_5d_pct_points": _round(technical.get("return_5d_pct") - benchmark_csi_5d) if technical.get("return_5d_pct") is not None and benchmark_csi_5d is not None else None,
            }
            row = {
                "code": code,
                **identity,
                "quote": quote,
                "intraday": intraday.get(code),
                "technical": technical,
                "relative_strength": relative_strength,
            }
            if identity["role"] == "holding":
                position = position_by_code[code]
                cost, shares = _number(position.get("cost")), _non_negative_number(position.get("shares")) or 0
                row["position"] = {
                    **{key: position.get(key) for key in (
                        "shares", "sellable_shares", "today_bought_shares", "cost", "opened_on",
                    )},
                    "market_value": _round(quote.get("last_price") * shares, 2) if quote.get("last_price") else None,
                    "unrealized_pnl_cny": _round((quote.get("last_price") - cost) * shares, 2) if quote.get("last_price") and cost else None,
                    "unrealized_pnl_pct": _pct_change(quote.get("last_price"), cost),
                }
            else:
                candidate = candidate_by_code[code]
                dynamic = {}
                for field in ("reference_price", "support_price", "resistance_price", "invalidation_price", "target_price"):
                    dynamic[f"distance_to_{field}_pct"] = _pct_change(quote.get("last_price"), _number(candidate.get(field)))
                row["candidate"] = {
                    **{key: candidate.get(key) for key in (
                        "sector", "bucket", "score", "score_breakdown", "technical_confirmation",
                        "reference_price", "support_price", "resistance_price", "invalidation_price",
                        "target_price", "risk_reward_ratio", "catalyst", "risk", "invalidation",
                        "admitted_on", "data_as_of", "replacement_eligible", "status",
                    )},
                    "dynamic": dynamic,
                }
            rows.append(row)

        candidate_rows = [item for item in rows if item["role"] == "candidate"]
        for metric_path, rank_key in (
            (("technical", "return_5d_pct"), "pool_rank_by_5d_return"),
            (("quote", "change_pct"), "pool_rank_by_intraday_change"),
            (("relative_strength", "vs_csi300_5d_pct_points"), "pool_rank_by_5d_relative_strength"),
        ):
            section, metric = metric_path
            ranked = sorted(
                [item for item in candidate_rows if item.get(section, {}).get(metric) is not None],
                key=lambda item: item[section][metric],
                reverse=True,
            )
            for index, item in enumerate(ranked, start=1):
                item.setdefault("pool_comparison", {})[rank_key] = index
                item["pool_comparison"][f"{rank_key}_of"] = len(ranked)

        tradeable_count = sum(item["quote"].get("tradeable", False) for item in rows)
        feature_coverage = {
            "tradeable_quote_symbols": tradeable_count,
            "intraday_feature_symbols": sum(bool((item.get("intraday") or {}).get("features")) for item in rows),
            "technical_feature_symbols": sum(bool(item.get("technical")) for item in rows),
            "relative_strength_symbols": sum(any(value is not None for value in item.get("relative_strength", {}).values()) for item in rows),
            "benchmark_quotes": sum(bool(item["quote"].get("tradeable")) for item in benchmark_rows),
        }
        packet = {
            "schema_version": 2,
            "kind": "node_market_packet",
            "node": node,
            "node_name": NODE_NAMES[node],
            "generated_at": now.isoformat(timespec="seconds"),
            "freshness_requirement_seconds": FRESHNESS_SECONDS,
            "scope": {"holdings": len(positions), "candidates": len(candidates), "symbols": len(rows)},
            "source_health": source_health,
            "market_context": market_context,
            "account": {
                "as_of": account.get("as_of"),
                "cash_available": account.get("cash_available"),
                "cash_frozen": account.get("cash_frozen"),
                "pending_orders": account.get("pending_orders", []),
            },
            "watchlist": {
                "status": watchlist.get("status"),
                "health": watchlist.get("health"),
                "valid_until": watchlist.get("valid_until"),
                "metadata": watchlist.get("metadata"),
            },
            "instruments": rows,
            "summary": {
                "tradeable_quotes": tradeable_count,
                "unavailable_or_stale_quotes": len(rows) - tradeable_count,
                "feature_coverage": feature_coverage,
                "instruction": "任一合规来源 tradeable=true 即可生成精确指令；腾讯主源合规时不调用备用报价源。技术和分时特征为本地确定性计算，Agent须引用事实后再解释。",
            },
            "timing": {"total_elapsed_ms": round((time.perf_counter() - started) * 1000)},
        }
        if persist:
            self._save_cache(quotes, intraday, kline_cache, benchmark_quotes)
            path = state.RECORDS_DIR / "market_packets" / now.strftime("%Y-%m-%d") / f"{now:%H%M%S}_{node.replace(':', '')}.json"
            packet["packet_path"] = str(path)
            packet["timing"] = {"total_elapsed_ms": round((time.perf_counter() - started) * 1000)}
            state._write_json(path, packet)
        else:
            packet["packet_path"] = None
            packet["ephemeral"] = True
        packet["timing"] = {"total_elapsed_ms": round((time.perf_counter() - started) * 1000)}
        return packet
    @staticmethod
    def _runtime_config() -> dict[str, Any]:
        config_path = state.ROOT / "config" / "runtime.json"
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _fetch_tencent_latest_batched_profile(self, codes: list[str]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        """Fetch bounded Tencent quote batches concurrently and keep per-batch evidence."""
        cfg = self._runtime_config()
        try:
            batch_size = max(1, int(cfg.get("tencent_batch_size", 50)))
            max_workers = max(1, int(cfg.get("tencent_batch_workers", 4)))
        except (TypeError, ValueError):
            batch_size, max_workers = 50, 4
        unique_codes = list(dict.fromkeys(str(code) for code in codes))
        batches = [unique_codes[start:start + batch_size] for start in range(0, len(unique_codes), batch_size)]
        result: dict[str, dict[str, Any]] = {}
        batch_rows: list[dict[str, Any]] = []
        started = time.perf_counter()
        if batches:
            with ThreadPoolExecutor(max_workers=min(max_workers, len(batches))) as executor:
                futures = {}
                for index, batch in enumerate(batches, start=1):
                    batch_started = time.perf_counter()
                    future = executor.submit(self._fetch_tencent_latest, batch)
                    futures[future] = (index, batch, batch_started)
                for future in as_completed(futures):
                    index, batch, batch_started = futures[future]
                    try:
                        fetched = future.result()
                        result.update(fetched)
                        batch_rows.append({
                            "batch": index,
                            "requested": len(batch),
                            "returned": len(fetched),
                            "elapsed_ms": round((time.perf_counter() - batch_started) * 1000),
                            "status": "online" if len(fetched) == len(batch) else "partial",
                            "codes": batch,
                        })
                    except Exception as exc:
                        batch_rows.append({
                            "batch": index,
                            "requested": len(batch),
                            "returned": 0,
                            "elapsed_ms": round((time.perf_counter() - batch_started) * 1000),
                            "status": "offline",
                            "error_type": type(exc).__name__,
                            "error_detail": str(exc)[:240],
                            "codes": batch,
                        })
        batch_rows.sort(key=lambda item: item["batch"])
        failures = [code for item in batch_rows if item["status"] == "offline" for code in item["codes"]]
        profile = {
            "status": "online" if unique_codes and len(result) == len(unique_codes) else "degraded" if result else "offline",
            "requested_symbols": len(unique_codes),
            "returned_symbols": len(result),
            "coverage": round(len(result) / len(unique_codes), 4) if unique_codes else 1.0,
            "batch_size": batch_size,
            "batch_count": len(batches),
            "workers": min(max_workers, len(batches)) if batches else 0,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "failed_codes": failures,
            "batches": batch_rows,
        }
        return result, profile

    def _fetch_tencent_latest_batched(self, codes: list[str]) -> dict[str, dict[str, Any]]:
        """Compatibility wrapper for callers that only need the quote mapping."""
        return self._fetch_tencent_latest_batched_profile(codes)[0]

    def _fetch_direct_sector_snapshot(self, sector_names: set[str]) -> dict[str, Any]:
        if not sector_names:
            return {
                "status": "not_requested",
                "source": "腾讯申万二级行业总体行情",
                "sectors": [],
                "requested_sectors": 0,
                "available_sectors": 0,
                "elapsed_ms": 0,
            }
        try:
            return self.sector_client.fetch_direct_summary(sorted(sector_names))
        except Exception as exc:
            return {
                "status": "unavailable",
                "source": "腾讯申万二级行业总体行情",
                "sectors": [],
                "requested_sectors": len(sector_names),
                "available_sectors": 0,
                "hard_filter_eligible_sectors": 0,
                "elapsed_ms": 0,
                "error_type": type(exc).__name__,
                "error_detail": str(exc)[:240],
            }

    def _sector_context(
        self,
        tracked_sector_names: set[str],
        now: datetime,
        direct_snapshot: dict[str, Any] | None = None,
        market_date_confirmed: bool = False,
    ) -> dict[str, Any]:
        """Normalize source-provided sector aggregates; never load constituents."""
        direct_snapshot = direct_snapshot or {}
        direct_by_name = {
            str(item.get("name")): item
            for item in direct_snapshot.get("sectors", [])
            if item.get("name")
        }
        rows = []
        for name in sorted(tracked_sector_names):
            direct = direct_by_name.get(name) or {}
            advancers = direct.get("advancers")
            constituent_count = direct.get("constituent_count")
            eligible = bool(direct.get("hard_filter_eligible")) and market_date_confirmed
            reason = direct.get("reason") or "板块总体数据不可用。"
            if direct.get("hard_filter_eligible") and not market_date_confirmed:
                reason = "腾讯指数未确认当前交易日，直接板块总体数据只能作背景。"
            rows.append({
                "name": name,
                "status": "ready" if eligible else str(direct.get("status") or "unavailable"),
                "source": direct_snapshot.get("source", "腾讯申万二级行业总体行情"),
                "source_sector_code": direct.get("source_sector_code"),
                "captured_at": direct_snapshot.get("captured_at"),
                "data_as_of": direct_snapshot.get("data_as_of"),
                "date_basis": direct_snapshot.get("date_basis"),
                "change_pct": direct.get("change_pct"),
                "turnover_100m_cny": direct.get("turnover_100m_cny"),
                "net_flow_100m_cny": direct.get("net_flow_100m_cny"),
                "advancers": advancers,
                "constituent_count": constituent_count,
                "non_advancers": direct.get("non_advancers"),
                "advance_ratio": direct.get("advance_ratio"),
                "leader": direct.get("leader"),
                "leader_code": direct.get("leader_code"),
                "leader_price": direct.get("leader_price"),
                "leader_change_pct": direct.get("leader_change_pct"),
                "return_5d_pct": direct.get("return_5d_pct"),
                "return_20d_pct": direct.get("return_20d_pct"),
                "return_60d_pct": direct.get("return_60d_pct"),
                "return_52w_pct": direct.get("return_52w_pct"),
                "return_ytd_pct": direct.get("return_ytd_pct"),
                "hard_filter_available": eligible,
                "reason": reason,
            })
        ready = sum(item["hard_filter_available"] for item in rows)
        return {
            "status": "ready" if rows and ready == len(rows) else "degraded" if ready else "unavailable",
            "hard_filter_status": "ready" if rows and ready == len(rows) else "degraded" if ready else "unavailable",
            "method": "直接读取数据源的板块总体行情；不抓取成分股，不做本地板块行情计算",
            "source": direct_snapshot.get("source", "腾讯申万二级行业总体行情"),
            "captured_at": direct_snapshot.get("captured_at"),
            "data_as_of": direct_snapshot.get("data_as_of"),
            "date_basis": direct_snapshot.get("date_basis"),
            "requested_sectors": len(rows),
            "available_sectors": sum(item.get("status") != "unavailable" for item in rows),
            "hard_filter_eligible_sectors": ready,
            "market_date_confirmed_by_tencent_index": market_date_confirmed,
            "sectors": rows,
            "trade_rule": "仅 hard_filter_available=true 的直接板块总体数据可用于板块硬条件；失败或盘前时冻结依赖板块条件的新买入。",
        }

    def monitor_snapshot(self, codes: list[str]) -> dict[str, dict[str, Any]]:
        """Return a small Tencent-only snapshot for monitor polling."""
        now = shanghai_now()
        raw_codes = list(dict.fromkeys(str(code) for code in codes))
        benchmark = "sh000300"
        try:
            fetched = self._fetch_tencent_latest_batched(raw_codes + [benchmark])
        except Exception:
            fetched = {}
        csi = self._combined_quote("000300", fetched.get("000300"), None, None, now)
        result: dict[str, dict[str, Any]] = {}
        for code in raw_codes:
            quote = self._combined_quote(code, fetched.get(code), None, None, now)
            relative = None
            if quote.get("tradeable") and csi.get("tradeable"):
                relative = _round(float(quote["change_pct"]) - float(csi["change_pct"]))
            result[code] = {
                "quote": quote,
                "relative_strength": {"vs_csi300_intraday_pct_points": relative},
            }
        return result

    def build(self, trigger: str = "manual", include_intraday: bool = True, persist: bool = True) -> dict[str, Any]:
        """Build a dynamic decision packet with direct sector aggregates."""
        started = time.perf_counter()
        now = shanghai_now()
        today = now.date().isoformat()
        trigger = str(trigger or "manual")
        account, watchlist = state.get_account(), state.get_watchlist()
        positions = list(account.get("positions", []))
        candidates = list(watchlist.get("candidates", []))
        position_by_code = {str(item["code"]): item for item in positions}
        candidate_by_code = {str(item["code"]): item for item in candidates}
        identities: dict[str, dict[str, Any]] = {}
        for position in positions:
            identities[str(position["code"])] = {
                "role": "holding", "name": position.get("name", ""), "sector": position.get("sector", ""),
            }
        for candidate in candidates:
            identities.setdefault(str(candidate["code"]), {
                "role": "candidate", "name": candidate.get("name", ""), "sector": candidate.get("sector", ""),
            })
        tracked_codes = list(identities)
        tracked_sectors = {str(item.get("sector")) for item in identities.values() if item.get("sector")}
        benchmark_codes = list(MARKET_BENCHMARKS)
        core_quote_codes = list(dict.fromkeys(tracked_codes + benchmark_codes))
        cache = self._load_cache()
        source_health: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=2) as executor:
            quote_future = executor.submit(self._fetch_tencent_latest_batched_profile, core_quote_codes)
            direct_future = executor.submit(self._fetch_direct_sector_snapshot, tracked_sectors)
            try:
                fetched, quote_profile = quote_future.result()
            except Exception as exc:
                fetched = {}
                quote_profile = {
                    "status": "offline", "requested_symbols": len(core_quote_codes), "returned_symbols": 0,
                    "coverage": 0.0, "elapsed_ms": 0, "failed_codes": core_quote_codes,
                    "error_type": type(exc).__name__, "error_detail": str(exc)[:240],
                }
            direct_sector_snapshot = direct_future.result()
        source_health.append({
            "name": "腾讯核心批量实时行情",
            "status": quote_profile["status"],
            "symbols": quote_profile["returned_symbols"],
            **quote_profile,
        })
        source_health.append({
            "name": "非腾讯盘中实时源", "status": "disabled", "symbols": 0,
            "policy": "个股、指数和板块均不自动切换到非腾讯源。",
        })
        source_health.append({
            "name": direct_sector_snapshot.get("source", "腾讯申万二级行业总体行情"),
            "status": direct_sector_snapshot.get("status", "unavailable"),
            "symbols": direct_sector_snapshot.get("available_sectors", 0),
            "requested_symbols": direct_sector_snapshot.get("requested_sectors", len(tracked_sectors)),
            "elapsed_ms": direct_sector_snapshot.get("elapsed_ms", 0),
            "source_reported_eligible_symbols": direct_sector_snapshot.get("hard_filter_eligible_sectors", 0),
            "method": direct_sector_snapshot.get("method"),
            "error_type": direct_sector_snapshot.get("error_type"),
            "error_detail": direct_sector_snapshot.get("error_detail"),
        })
        cached_quotes = cache.get("quotes") or {}
        quotes = {
            code: self._combined_quote(code, fetched.get(code), None, cached_quotes.get(code), now)
            for code in tracked_codes
        }
        benchmark_quotes = {
            full[2:]: self._combined_quote(full[2:], fetched.get(full[2:]), None, (cache.get("market") or {}).get(full[2:]), now)
            for full in benchmark_codes
        }

        kline_cache = dict(cache.get("klines") or {})
        history_bars: dict[str, list[dict[str, Any]]] = {}
        history_codes = tracked_codes + benchmark_codes
        missing = []
        for requested in history_codes:
            raw = requested[2:] if requested.startswith(("sh", "sz")) else requested
            entry = kline_cache.get(raw) or {}
            if entry.get("fetched_on") == today and isinstance(entry.get("bars"), list):
                history_bars[raw] = entry["bars"]
            else:
                missing.append(requested)
        errors = []
        if missing:
            with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_REQUESTS, len(missing))) as executor:
                futures = {executor.submit(self._fetch_tencent_kline, code): code for code in missing}
                for future in as_completed(futures):
                    requested = futures[future]
                    raw = requested[2:] if requested.startswith(("sh", "sz")) else requested
                    try:
                        bars = future.result()
                        if bars:
                            history_bars[raw] = bars
                            kline_cache[raw] = {"fetched_on": today, "bars": bars}
                        elif isinstance((kline_cache.get(raw) or {}).get("bars"), list):
                            history_bars[raw] = kline_cache[raw]["bars"]
                    except Exception as exc:
                        errors.append(f"{raw}:{type(exc).__name__}")
                        if isinstance((kline_cache.get(raw) or {}).get("bars"), list):
                            history_bars[raw] = kline_cache[raw]["bars"]
        source_health.append({
            "name": "腾讯前复权日K", "status": "online" if history_bars else "offline",
            "symbols": len(history_bars), "errors": errors[:8],
        })

        intraday: dict[str, Any] = {}
        intraday_policy = intraday_expectation(now)
        effective_include_intraday = bool(include_intraday and intraday_policy["expected"])
        if not intraday_policy["expected"]:
            intraday = {
                code: {
                    "ok": False,
                    "status": "not_expected",
                    "source": "腾讯分时",
                    "tradeable": False,
                    "reason": intraday_policy["reason"],
                }
                for code in tracked_codes
            }
        elif effective_include_intraday and tracked_codes:
            with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_REQUESTS, len(tracked_codes))) as executor:
                futures = {executor.submit(self._fetch_intraday, code, quotes.get(code)): code for code in tracked_codes}
                for future in as_completed(futures):
                    code = futures[future]
                    try:
                        intraday[code] = future.result()
                    except Exception as exc:
                        intraday[code] = {
                            "ok": False,
                            "status": "unavailable",
                            "source": "腾讯分时",
                            "tradeable": False,
                            "error_type": type(exc).__name__,
                            "error_detail": str(exc)[:240],
                        }
        intraday_status = (
            "not_expected"
            if not intraday_policy["expected"]
            else "not_requested"
            if not include_intraday
            else "online"
            if any(item.get("ok") for item in intraday.values())
            else "offline"
        )
        source_health.append({
            "name": "腾讯分时", "status": intraday_status,
            "symbols": sum(bool(item.get("ok")) for item in intraday.values()),
            "requested": effective_include_intraday,
            "reason": intraday_policy["reason"] if intraday_status == "not_expected" else None,
            "errors": [
                {"code": code, "type": item.get("error_type"), "detail": item.get("error_detail")}
                for code, item in intraday.items()
                if item.get("status") == "unavailable"
            ][:8],
        })

        benchmark_rows = []
        for full_code, name in MARKET_BENCHMARKS.items():
            raw = full_code[2:]
            quote = benchmark_quotes[raw]
            technical = _technical_features(history_bars.get(raw, []), quote, now)
            benchmark_rows.append({"code": raw, "name": name, "quote": quote, "technical": technical})
        live_changes = [float(item["quote"]["change_pct"]) for item in benchmark_rows if item["quote"].get("tradeable")]
        positive = sum(value > 0 for value in live_changes)
        if len(live_changes) < 2:
            regime = "数据不足"
        elif positive == len(live_changes):
            regime = "指数普涨"
        elif positive >= 2:
            regime = "指数偏强但有分化"
        elif positive == 1:
            regime = "指数偏弱且有分化"
        else:
            regime = "指数普跌"
        market_context = {
            "regime": regime,
            "positive_benchmarks": positive,
            "average_change_pct": _round(sum(live_changes) / len(live_changes)) if live_changes else None,
            "benchmarks": benchmark_rows,
            "scope_note": "仅以腾讯指数行情判断大盘环境，不臆造全市场宽度。",
        }
        market_date_confirmed = any(
            str((item.get("quote") or {}).get("quote_timestamp") or "").startswith(today)
            for item in benchmark_rows
        )
        market_context["trade_date_confirmed"] = market_date_confirmed
        csi = next((item for item in benchmark_rows if item["code"] == "000300"), {})
        csi_change = (csi.get("quote") or {}).get("change_pct")
        csi_5d = (csi.get("technical") or {}).get("return_5d_pct")
        rows = []
        for code, identity in identities.items():
            quote = quotes[code]
            technical = _technical_features(history_bars.get(code, []), quote, now, position_by_code.get(code, {}).get("opened_on"))
            relative = {
                "vs_csi300_intraday_pct_points": _round(float(quote["change_pct"]) - float(csi_change)) if quote.get("change_pct") is not None and csi_change is not None else None,
                "vs_csi300_5d_pct_points": _round(float(technical["return_5d_pct"]) - float(csi_5d)) if technical.get("return_5d_pct") is not None and csi_5d is not None else None,
            }
            row = {"code": code, **identity, "quote": quote, "intraday": intraday.get(code), "technical": technical, "relative_strength": relative}
            if identity["role"] == "holding":
                position = position_by_code[code]
                cost, shares = _number(position.get("cost")), int(position.get("shares", 0))
                row["position"] = {
                    **{key: position.get(key) for key in ("shares", "sellable_shares", "today_bought_shares", "cost", "opened_on", "sector")},
                    "market_value": _round(float(quote["last_price"]) * shares, 2) if quote.get("last_price") else None,
                    "unrealized_pnl_cny": _round((float(quote["last_price"]) - float(cost)) * shares, 2) if quote.get("last_price") and cost else None,
                    "unrealized_pnl_pct": _pct_change(quote.get("last_price"), cost),
                }
            else:
                candidate = candidate_by_code[code]
                row["candidate"] = dict(candidate)
            rows.append(row)
        sector_context = self._sector_context(
            tracked_sectors,
            now,
            direct_snapshot=direct_sector_snapshot,
            market_date_confirmed=market_date_confirmed,
        )
        for row in rows:
            row["sector_market"] = next((item for item in sector_context.get("sectors", []) if item.get("name") == row.get("sector")), None)
        tradeable_count = sum(bool(item["quote"].get("tradeable")) for item in rows)
        packet = {
            "schema_version": 5,
            "kind": "dynamic_market_packet",
            "trigger": trigger,
            "trigger_name": ANALYSIS_TRIGGERS.get(trigger, "自定义触发"),
            "generated_at": now.isoformat(timespec="seconds"),
            "freshness_requirement_seconds": FRESHNESS_SECONDS,
            "scope": {
                "holdings": len(positions),
                "candidates": len(candidates),
                "tracked_symbols": len(rows),
                "sector_direct_symbols": len(tracked_sectors),
            },
            "source_policy": {
                "intraday_primary": "腾讯",
                "intraday_expected": intraday_policy["expected"],
                "intraday_expectation_reason": intraday_policy["reason"],
                "automatic_fallback": False,
                "on_tencent_failure": "标记不可交易并等待下次刷新",
                "sector_mode": "直接板块总体行情；不请求成分股，不做本地计算",
                "direct_sector_hard_filter_eligible": sector_context.get("hard_filter_eligible_sectors", 0),
            },
            "source_health": source_health,
            "market_context": market_context,
            "sector_context": sector_context,
            "account": {"as_of": account.get("as_of"), "cash_available": account.get("cash_available"), "cash_frozen": account.get("cash_frozen"), "pending_orders": account.get("pending_orders", [])},
            "watchlist": {"status": watchlist.get("status"), "health": watchlist.get("health"), "valid_until": watchlist.get("valid_until"), "metadata": watchlist.get("metadata")},
            "instruments": rows,
            "summary": {
                "tradeable_quotes": tradeable_count,
                "unavailable_or_stale_quotes": len(rows) - tradeable_count,
                "instruction": (
                    "盘前只使用腾讯集合竞价报价、前一交易日日K和账户状态；不使用分时、VWAP、量比或盘中入场逻辑。"
                    if not intraday_policy["expected"]
                    else "只有腾讯 fresh 且 tradeable=true 的个股可生成精确交易建议；直接板块总体数据失败或字段不完整时板块硬条件不可用。"
                ),
            },
            "timing": {"total_elapsed_ms": round((time.perf_counter() - started) * 1000)},
        }
        if persist:
            self._save_cache(quotes, intraday, kline_cache, benchmark_quotes)
            safe_trigger = re.sub(r"[^A-Za-z0-9_-]+", "_", trigger)[:40] or "manual"
            path = state.RECORDS_DIR / "market_packets" / today / f"{now:%H%M%S}_{safe_trigger}.json"
            packet["packet_path"] = str(path)
            state._write_json(path, packet)
        else:
            packet["packet_path"] = None
            packet["ephemeral"] = True
        packet["timing"] = {"total_elapsed_ms": round((time.perf_counter() - started) * 1000)}
        return packet
