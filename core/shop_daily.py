"""店铺每日流量 / 广告日报（v2.13）。

数据源是赛狐「数据 → 产品分析 → 店铺」页背后的接口：

    POST https://www.sellfox.com/api/product/analysis/shop/page.json

它和广告接口不在同一个网关（广告走 ``/api/gw/sellfox/sellfox-cpc/...``，
这里走普通的 ``/api/...``），但用的是同一份 Cookie，所以不需要额外授权。

一次请求按「店铺 × 日期区间」返回，把 ``startDate`` 与 ``endDate`` 设成同一天，
就能拿到当天**所有店铺**的四项指标：

- ``adClicksThis``   广告点击次数
- ``sessionsThis``   会话次数（Sessions，来自亚马逊业务报告）
- ``adCostThis``     广告花费（SP + SB + SD 合计，币种跟随店铺）
- ``pageViewThis``   浏览量（PV）

四个衍生指标由前端按行计算：
1. 广告点击次数
2. 会话次数
3. (会话 - 点击) / 会话
4. 广告花费 / 会话

存储口径
--------
主键 ``(shop_id, report_date)``，重复同步覆盖旧值——赛狐会回溯修正历史业务报告。
数据一旦入库就与 Cookie 无关：换账号、Cookie 过期都还能在页面上查到。
"""

from __future__ import annotations

import contextlib
import datetime
from typing import Any, Dict, Iterable, List, Optional

from core import database as db
from core import sellfox_client as sf

# 普通 web 网关（不是广告用的 /api/gw/sellfox/...）
WEB_API = "https://www.sellfox.com/api"
_REPORT_PATH = "product/analysis/shop/page.json"

# 店铺维度报表的固定参数。asinType=6 表示「店铺」维度；
# searchType 仍传 asin 是照抄网页端的请求，改动会被服务端判为非法组合。
_BODY_TMPL: Dict[str, Any] = {
    "pageNo": 1,
    "pageSize": 200,
    "tagId": "",
    "asinType": 6,
    "currency": "",
    "devIds": "",
    "operatorIds": "",
    "marketplaceIds": "",
    "searchContent": "",
    "searchType": "asin",
    "searchMode": "exact",
    "shopIds": "",
    "statTimeType": 1,
    "fullCid": "",
    "brandIds": [],
    "compareType": 0,
    "userIdList": [],
    "shopIdList": [],
    "marketplaceIdList": [],
    "newDevIds": [],
    "newOperatorIds": [],
    "orderBy": "sessionsThis",
    "desc": True,
}

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS shop_daily_traffic (
    shop_id      TEXT NOT NULL,
    report_date  TEXT NOT NULL,
    shop_name    TEXT NOT NULL DEFAULT '',
    ad_clicks    INTEGER NOT NULL DEFAULT 0,
    ad_cost      REAL NOT NULL DEFAULT 0,
    sessions     INTEGER NOT NULL DEFAULT 0,
    page_views   INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (shop_id, report_date)
)
"""


# ---------------------------------------------------------------------------
# 建表 / 基础工具
# ---------------------------------------------------------------------------
def ensure_table() -> None:
    """建表（幂等）。"""
    conn = db.get_conn()
    conn.execute(_TABLE_DDL)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_shop_daily_date ON shop_daily_traffic (report_date)"
    )
    conn.commit()


def _num(value: Any, cast=float) -> float:
    try:
        if value is None or value == "":
            return cast(0)
        return cast(value)
    except (TypeError, ValueError):
        return cast(0)


def _date_range(start: str, end: str) -> List[str]:
    """闭区间日期列表；参数非法时抛 ValueError。"""
    d0 = datetime.date.fromisoformat(start)
    d1 = datetime.date.fromisoformat(end)
    if d1 < d0:
        d0, d1 = d1, d0
    out: List[str] = []
    cur = d0
    while cur <= d1:
        out.append(cur.isoformat())
        cur += datetime.timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------
def fetch_day(day: str) -> List[Dict[str, Any]]:
    """拉取某一天、当前账号可见的全部店铺指标。"""
    body = dict(_BODY_TMPL)
    body["startDate"] = day
    body["endDate"] = day
    resp = sf.post_json(_REPORT_PATH, body, base=WEB_API)
    data = resp.get("data") or {}
    if isinstance(data, dict):
        return data.get("rows") or []
    return []


def upsert_rows(day: str, rows: Iterable[Dict[str, Any]]) -> int:
    """把一天的店铺指标写库，返回写入条数。"""
    conn = db.get_conn()
    now = datetime.datetime.now().isoformat(timespec="seconds")
    n = 0
    for r in rows:
        shop_id = str(r.get("shopId") or "").strip()
        if not shop_id:
            continue
        conn.execute(
            """
            INSERT INTO shop_daily_traffic
                (shop_id, report_date, shop_name, ad_clicks, ad_cost,
                 sessions, page_views, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(shop_id, report_date) DO UPDATE SET
                shop_name  = excluded.shop_name,
                ad_clicks  = excluded.ad_clicks,
                ad_cost    = excluded.ad_cost,
                sessions   = excluded.sessions,
                page_views = excluded.page_views,
                updated_at = excluded.updated_at
            """,
            (
                shop_id,
                day,
                str(r.get("shopName") or ""),
                int(_num(r.get("adClicksThis"), int)),
                float(_num(r.get("adCostThis"), float)),
                int(_num(r.get("sessionsThis"), int)),
                int(_num(r.get("pageViewThis"), int)),
                now,
            ),
        )
        n += 1
    conn.commit()
    return n


def sync_range(
    start: str,
    end: str,
    account_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """按日期区间同步。

    逐个账号切换后再逐天请求：赛狐一个账号只能看到自己权限内的店铺，
    多账号场景必须都跑一遍，才能把不同账号下的店铺数据收进同一张表。
    同一天被多个账号重复拉到也无所谓，主键会覆盖成最后一次的值。
    """
    ensure_table()
    dates = _date_range(start, end)

    accounts = sf.list_accounts()
    if account_ids:
        wanted = set(account_ids)
        accounts = [a for a in accounts if a.get("id") in wanted]

    results: List[Dict[str, Any]] = []
    total_rows = 0
    errors: List[str] = []
    shops: Dict[str, str] = {}

    for acc in accounts or [{}]:
        acc_id = acc.get("id") or ""
        ctx = sf.use_account(acc_id) if acc_id else contextlib.nullcontext()
        with ctx:
            for day in dates:
                try:
                    rows = fetch_day(day)
                except Exception as exc:  # noqa: BLE001 - 单日失败不影响其他日期
                    msg = f"{day}（{acc.get('label') or acc_id or '当前账号'}）：{exc}"
                    errors.append(msg[:200])
                    results.append({"date": day, "ok": False, "error": str(exc)[:200]})
                    continue
                written = upsert_rows(day, rows)
                total_rows += written
                for r in rows:
                    sid = str(r.get("shopId") or "")
                    if sid:
                        shops[sid] = str(r.get("shopName") or "")
                results.append({"date": day, "ok": True, "rows": written})

    return {
        "ok": True,
        "start_date": dates[0],
        "end_date": dates[-1],
        "days": len(dates),
        "accounts": len(accounts),
        "shops": len(shops),
        "rows": total_rows,
        "errors": errors,
        "results": results,
    }


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
def query_range(start: str, end: str) -> List[Dict[str, Any]]:
    ensure_table()
    conn = db.get_conn()
    rows = conn.execute(
        """
        SELECT shop_id, report_date, shop_name, ad_clicks, ad_cost,
               sessions, page_views, updated_at
        FROM shop_daily_traffic
        WHERE report_date BETWEEN ? AND ?
        ORDER BY report_date
        """,
        (start, end),
    ).fetchall()
    return [dict(r) for r in rows]


def build_month_table(month: str, include_empty: bool = False) -> Dict[str, Any]:
    """组装「店铺 × 日期」表格数据。

    ``month`` 形如 ``2026-11``；为空时用库里最新数据所在的月份。
    ``include_empty=True`` 时把还没有数据的店铺也列出来（占位空行），
    默认只返回有数据店铺，避免整页空白。
    """
    ensure_table()
    conn = db.get_conn()

    if not month:
        row = conn.execute("SELECT MAX(report_date) AS d FROM shop_daily_traffic").fetchone()
        month = (row["d"] if row and row["d"] else datetime.date.today().isoformat())[:7]

    year, mon = (int(x) for x in month.split("-")[:2])
    first = datetime.date(year, mon, 1)
    next_month = datetime.date(year + (mon == 12), 1 if mon == 12 else mon + 1, 1)
    last = next_month - datetime.timedelta(days=1)
    days = [first + datetime.timedelta(days=i) for i in range((last - first).days + 1)]
    day_strs = [d.isoformat() for d in days]

    raw = query_range(day_strs[0], day_strs[-1])

    # 店铺中文名优先取 shops 表里的别名，保证和别的模块口径一致
    alias = {
        str(s.get("shop_id")): (s.get("alias") or s.get("shop_name") or "")
        for s in db.list_shops()
    }

    by_shop: Dict[str, Dict[str, Any]] = {}
    for r in raw:
        sid = str(r["shop_id"])
        entry = by_shop.setdefault(
            sid,
            {
                "shop_id": sid,
                "shop_name": r.get("shop_name") or alias.get(sid) or sid,
                "cells": {},
            },
        )
        if not entry["shop_name"] and r.get("shop_name"):
            entry["shop_name"] = r["shop_name"]
        entry["cells"][r["report_date"]] = {
            "clicks": int(r["ad_clicks"] or 0),
            "sessions": int(r["sessions"] or 0),
            "cost": round(float(r["ad_cost"] or 0), 2),
            "pv": int(r["page_views"] or 0),
        }

    shops = []
    for entry in by_shop.values():
        name = entry["shop_name"]
        entry["display_name"] = alias.get(entry["shop_id"]) or name
        # 派生指标在这里算，前端只管展示，避免两边口径漂移
        for day, cell in entry["cells"].items():
            sess = cell["sessions"]
            cell["ratio"] = round((sess - cell["clicks"]) / sess, 4) if sess else None
            cell["cost_per_session"] = round(cell["cost"] / sess, 2) if sess else None
            # 有广告点击却零会话：广告数据已到、业务报告还没同步到这一天。
            # 广告点击必然带来会话，所以这种组合只可能是上游缺数，不是真的 0。
            cell["pending"] = sess == 0 and cell["clicks"] > 0
        shops.append(entry)

    # 没有数据的店铺：默认不显示，勾选后才占位
    if include_empty:
        for shop in db.list_shops():
            sid = str(shop.get("shop_id") or "")
            if not sid or sid in by_shop:
                continue
            shops.append(
                {
                    "shop_id": sid,
                    "shop_name": shop.get("shop_name") or sid,
                    "display_name": alias.get(sid) or shop.get("shop_name") or sid,
                    "cells": {},
                }
            )

    # A→Z 排序：用展示名（别名已带 A/B/C… 前缀），保证和其他模块一致
    shops.sort(key=lambda x: (x["display_name"] or x["shop_name"]).lower())

    last_sync = conn.execute(
        "SELECT MAX(updated_at) AS t FROM shop_daily_traffic WHERE report_date BETWEEN ? AND ?",
        (day_strs[0], day_strs[-1]),
    ).fetchone()

    # 全月「业务报告未到」的日期：这一天所有有数据的店铺都是零会话
    pending_days = [
        d
        for d in day_strs
        if any(
            s["cells"].get(d, {}).get("pending")
            for s in shops
        )
    ]

    return {
        "ok": True,
        "month": month,
        "days": day_strs,
        "shops": shops,
        "summary": {
            "shops": len(shops),
            "days": len(day_strs),
            "cells": sum(len(s["cells"]) for s in shops),
            "pending_days": pending_days,
            "last_sync": (last_sync["t"] if last_sync and last_sync["t"] else ""),
        },
    }


def available_months(limit: int = 12) -> List[str]:
    ensure_table()
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT DISTINCT substr(report_date, 1, 7) AS m FROM shop_daily_traffic ORDER BY m DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [r["m"] for r in rows if r["m"]]
