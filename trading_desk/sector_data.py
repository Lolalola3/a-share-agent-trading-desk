"""Tencent direct sector-level market data.

The trading desk never requests sector constituents here.  Tencent's industry
rank endpoint supplies one aggregate row per Shenwan industry, including the
sector move, turnover, main-fund flow, rising-stock ratio, leader and multi-day
performance.
"""
from __future__ import annotations

import json
import re
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

try:
    import requests
except ImportError:  # pragma: no cover - urllib is the dependency-free fallback
    requests = None

from . import state


SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
TENCENT_SECTOR_URL = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank"
TENCENT_REFERER = "https://stockapp.finance.qq.com/mstats/#mod=list&id=hy_second&module=hy&type=second"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
)
_HTTP_SESSION: Any | None = None
_HTTP_LOCK = threading.Lock()


class SectorDataError(RuntimeError):
    pass


def shanghai_now() -> datetime:
    return datetime.now(SHANGHAI)


def load_config() -> dict[str, Any]:
    path = state.ROOT / "config" / "sector_data.json"
    defaults = {
        "schema_version": 3,
        "direct_source": {
            "enabled": True,
            "source": "tencent_shenwan_level2_rank",
            "timeout_seconds": 6.0,
            "max_attempts": 1,
            "hard_filter_start_time": "09:30",
            "board_type": "hy2",
            "row_limit": 200,
        },
        "sector_aliases": {"种植业与林业": "种植业"},
    }
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return defaults
    return {
        **defaults,
        **loaded,
        "direct_source": {**defaults["direct_source"], **dict(loaded.get("direct_source") or {})},
        "sector_aliases": {**defaults["sector_aliases"], **dict(loaded.get("sector_aliases") or {})},
    }


def _http_bytes(url: str, timeout: float) -> bytes:
    cfg = load_config()["direct_source"]
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json,text/plain,*/*",
        "Referer": TENCENT_REFERER,
        "Connection": "keep-alive",
    }
    attempts = max(1, int(cfg.get("max_attempts", 1)))
    if requests is not None:
        global _HTTP_SESSION
        with _HTTP_LOCK:
            if _HTTP_SESSION is None:
                _HTTP_SESSION = requests.Session()
                _HTTP_SESSION.trust_env = False
            last_error: Exception | None = None
            for attempt in range(attempts):
                try:
                    response = _HTTP_SESSION.get(
                        url,
                        headers=headers,
                        timeout=(max(1.0, timeout), max(2.0, timeout)),
                        allow_redirects=True,
                    )
                    response.raise_for_status()
                    if not response.content:
                        raise SectorDataError("腾讯板块总体数据源返回空响应。")
                    return bytes(response.content)
                except Exception as exc:
                    last_error = exc
                    if attempt + 1 < attempts:
                        time.sleep(0.2)
        assert last_error is not None
        raise SectorDataError(f"腾讯板块总体请求失败：{type(last_error).__name__}: {str(last_error)[:180]}") from last_error

    request = Request(url, headers=headers)
    context = ssl.create_default_context()
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context))
    try:
        with opener.open(request, timeout=timeout) as response:  # nosec B310 - fixed HTTPS host
            payload = response.read()
            if not payload:
                raise SectorDataError("腾讯板块总体数据源返回空响应。")
            return payload
    except Exception as exc:
        if isinstance(exc, SectorDataError):
            raise
        raise SectorDataError(f"腾讯板块总体请求失败：{type(exc).__name__}: {str(exc)[:180]}") from exc


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed


def _breadth(value: Any) -> tuple[int | None, int | None]:
    match = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", str(value or ""))
    if not match:
        return None, None
    advancers, total = int(match.group(1)), int(match.group(2))
    if total <= 0 or advancers < 0 or advancers > total:
        return None, None
    return advancers, total


class TencentSectorClient:
    def __init__(self, fetcher: Callable[[str, float], bytes] = _http_bytes) -> None:
        self.fetcher = fetcher

    def fetch_direct_summary(self, sector_names: list[str]) -> dict[str, Any]:
        """Fetch direct aggregate rows without requesting any constituents."""
        cfg = load_config()
        source_cfg = cfg["direct_source"]
        started = time.perf_counter()
        captured = shanghai_now()
        requested_names = list(dict.fromkeys(str(value).strip() for value in sector_names if str(value).strip()))
        if not requested_names:
            return {
                "status": "not_requested",
                "source": "腾讯申万二级行业总体行情",
                "sectors": [],
                "requested_sectors": 0,
                "available_sectors": 0,
                "elapsed_ms": 0,
            }
        if not source_cfg.get("enabled", True):
            return {
                "status": "disabled",
                "source": "腾讯申万二级行业总体行情",
                "sectors": [],
                "requested_sectors": len(requested_names),
                "available_sectors": 0,
                "elapsed_ms": 0,
            }

        query = urlencode({
            "board_type": str(source_cfg.get("board_type", "hy2")),
            "sort_type": "priceRatio",
            "direct": "down",
            "offset": 0,
            "count": int(source_cfg.get("row_limit", 200)),
        })
        timeout = float(source_cfg.get("timeout_seconds", 6.0))
        try:
            payload = json.loads(self.fetcher(f"{TENCENT_SECTOR_URL}?{query}", timeout).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SectorDataError(f"腾讯板块总体响应不是有效JSON：{type(exc).__name__}") from exc
        if int(payload.get("code", -1)) != 0:
            raise SectorDataError(f"腾讯板块总体接口返回错误码：{payload.get('code')}")
        raw_rows = ((payload.get("data") or {}).get("rank_list") or [])
        if not isinstance(raw_rows, list) or not raw_rows:
            raise SectorDataError("腾讯板块总体接口未返回行业排行。")

        rows: dict[str, dict[str, Any]] = {}
        for item in raw_rows:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            advancers, constituent_count = _breadth(item.get("zgb"))
            leader = item.get("lzg") if isinstance(item.get("lzg"), dict) else {}
            turnover_10k = _number(item.get("turnover"))
            net_flow_10k = _number(item.get("zljlr"))
            row = {
                "name": str(item["name"]),
                "source_sector_code": str(item.get("code") or ""),
                "change_pct": _number(item.get("zdf")),
                "turnover_100m_cny": round(turnover_10k / 10000, 4) if turnover_10k is not None else None,
                "net_flow_100m_cny": round(net_flow_10k / 10000, 4) if net_flow_10k is not None else None,
                "advancers": advancers,
                "constituent_count": constituent_count,
                "non_advancers": constituent_count - advancers if advancers is not None and constituent_count is not None else None,
                "advance_ratio": round(advancers / constituent_count, 4) if advancers is not None and constituent_count else None,
                "leader": leader.get("name"),
                "leader_code": leader.get("code"),
                "leader_price": _number(leader.get("zxj")),
                "leader_change_pct": _number(leader.get("zdf")),
                "return_5d_pct": _number(item.get("zdf_d5")),
                "return_20d_pct": _number(item.get("zdf_d20")),
                "return_60d_pct": _number(item.get("zdf_d60")),
                "return_52w_pct": _number(item.get("zdf_w52")),
                "return_ytd_pct": _number(item.get("zdf_y")),
            }
            required = ("change_pct", "turnover_100m_cny", "net_flow_100m_cny", "advancers", "constituent_count")
            row["status"] = "online" if all(row[field] is not None for field in required) else "incomplete"
            rows[row["name"]] = row

        aliases = cfg.get("sector_aliases", {})
        hard_filter_time = str(source_cfg.get("hard_filter_start_time", "09:30"))
        continuous_session = captured.strftime("%H:%M") >= hard_filter_time
        selected: list[dict[str, Any]] = []
        for requested_name in requested_names:
            source_name = str(aliases.get(requested_name, requested_name))
            row = dict(rows.get(source_name) or {
                "name": source_name,
                "status": "unavailable",
                "change_pct": None,
                "turnover_100m_cny": None,
                "net_flow_100m_cny": None,
                "advancers": None,
                "constituent_count": None,
                "non_advancers": None,
                "advance_ratio": None,
                "leader": None,
                "leader_code": None,
                "leader_price": None,
                "leader_change_pct": None,
            })
            row["name"] = requested_name
            row["source_name"] = source_name
            row["hard_filter_eligible"] = row.get("status") == "online" and continuous_session
            if row.get("status") != "online":
                row["reason"] = "腾讯板块总体数据缺失或关键字段不完整。"
            elif not continuous_session:
                row["reason"] = f"{hard_filter_time}前板块总体行情仍可能是前收盘数据，只作盘前背景。"
            else:
                row["reason"] = "腾讯板块总体关键字段完整，可用于本轮板块条件。"
            selected.append(row)

        available = sum(item.get("status") == "online" for item in selected)
        eligible = sum(bool(item.get("hard_filter_eligible")) for item in selected)
        return {
            "status": "online" if available == len(selected) else "degraded" if available else "unavailable",
            "source": "腾讯申万二级行业总体行情",
            "source_url": TENCENT_REFERER,
            "captured_at": captured.isoformat(timespec="seconds"),
            "data_as_of": captured.date().isoformat(),
            "date_basis": "采集日推断，并由腾讯指数报价日期交叉确认",
            "method": "直接读取腾讯板块总体行情；不请求成分股，不做本地板块行情计算",
            "hard_filter_start_time": hard_filter_time,
            "requested_sectors": len(selected),
            "available_sectors": available,
            "hard_filter_eligible_sectors": eligible,
            "sectors": selected,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        }
