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
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import state


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
MARKET_BENCHMARKS = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sh000300": "沪深300",
}
NODE_NAMES = {
    "09:08": "盘前准备", "09:22": "开盘计划", "10:30": "上午复核", "11:25": "午间复盘",
    "13:00": "午后开盘", "14:25": "尾盘确认", "14:50": "收盘风险", "15:05": "收盘复盘",
}


class MarketDataError(RuntimeError):
    """A source failed, but packet generation may still continue from cache."""


def shanghai_now() -> datetime:
    return datetime.now(SHANGHAI)


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
    # This project's Python transport has shown multi-second tails for Tencent
    # and TLS EOFs for Eastmoney under the active VPN. Windows-native curl is
    # already present on supported Windows versions and is materially faster
    # here. Prefer it for the two fixed public quote hosts with a hard process
    # timeout. Falling through to urllib after a curl failure caused stacked
    # DNS waits under VPN, so urllib is now only the no-curl portability path.
    fixed_market_host = "eastmoney.com" in url or "gtimg.cn" in url
    if fixed_market_host and (curl := shutil.which("curl.exe") or shutil.which("curl")):
        try:
            completed = subprocess.run(
                [curl, "--silent", "--show-error", "--location", "--connect-timeout", str(max(1, int(timeout))), "--max-time", str(max(2, int(timeout) + 1)), "--user-agent", USER_AGENT, "--output", "-", "--url", url],
                capture_output=True,
                check=False,
                timeout=max(3.0, timeout + 2.0),
            )
        except subprocess.TimeoutExpired as exc:
            raise MarketDataError(f"curl hard timeout after {timeout + 2.0:.1f}s") from exc
        if completed.returncode == 0 and completed.stdout:
            return completed.stdout
        error = completed.stderr.decode("utf-8", errors="replace").strip()
        raise MarketDataError(f"curl exit {completed.returncode}: {error[:120] or 'empty response'}")
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:  # nosec B310: fixed public HTTPS endpoints
            return response.read()
    except Exception as exc:
        raise MarketDataError(f"{type(exc).__name__}: {exc}") from exc


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
    def __init__(self, fetcher: Callable[[str, float], bytes] = _http_bytes, cache_path: Path | None = None) -> None:
        self.fetcher = fetcher
        self.cache_path = cache_path or state.STATE_DIR / "market_cache.json"

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
            query = urlencode({"secid": eastmoney_secid(code), "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58", "iscr": "0", "ndays": "1"})
            try:
                raw = self.fetcher(f"https://push2delay.eastmoney.com/api/qt/stock/trends2/get?{query}", INTRADAY_TIMEOUT_SECONDS)
                result = parse_eastmoney_intraday(json.loads(raw.decode("utf-8")), quote)
                if result:
                    return {"ok": True, "fallback_from": "腾讯分时", **result}
                raise MarketDataError("东方财富分时没有足够绘图点")
            except Exception as fallback_error:
                return {"ok": False, "source": "腾讯分时/东方财富分时备用", "error": f"{type(primary_error).__name__}; {type(fallback_error).__name__}"}

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


    def build(self, node: str, include_intraday: bool = True, persist: bool = True) -> dict[str, Any]:
        """Build the version-2 decision packet without mutating trading state.

        Quotes are fetched in one Tencent batch.  Intraday paths and missing
        daily histories are fetched concurrently; history is cached once per
        Shanghai calendar day.  Secondary quotes are requested only for
        symbols whose Tencent quote is absent or stale.
        """
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
