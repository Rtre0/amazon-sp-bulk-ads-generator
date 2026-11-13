"""SQLite 本地数据持久化层。

四张核心表：
1. ``shops`` — 店铺注册表（shop_id / shop_name / 站点 / 最后同步时间）
2. ``products`` — 商品库（服务于自动广告模块），(shop_id, msku, asin) 联合唯一索引，
   增量去重：已存在的旧商品直接跳过，不覆盖用户已微调过的中文品名。
3. ``ad_search_terms_daily`` — 每日「用户搜索词」维度报告（手动广告模块）。
4. ``ad_search_asin_daily`` — 每日「投放产生 ASIN」维度报告（手动广告模块）。
   两个维度来自赛狐同一接口的不同 ``searchQueryType``，必须分开存，不能混进一张表。

设计原则
--------
- 全部用标准库 ``sqlite3``，无需额外依赖（SQLAlchemy 留作可选）。
- 线程安全：每次操作 ``with get_conn()`` 自动 commit / close。
- 搜索词/ASIN 日报用 ``ON CONFLICT DO UPDATE``：赛狐会回溯修正历史报表，
  重复同步要能覆盖旧指标，同时保证入库计数真实。
- 查询支持「按天明细」和「区间汇总」两种口径；后者用 GROUP BY 把所选日期
  区间内的同一词/ASIN 汇总成一行，对齐赛狐区间报表，并用 ``order_dates``
  补上「出单时间」（赛狐区间行不带这个信息）。
- 所有表都带 ``created_at`` 时间戳，便于追踪入库时间。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 数据库文件位置
#   统一走 core.paths：设了 SELLFOX_HOME 就在项目外，否则仍在项目内（向后兼容）
# ---------------------------------------------------------------------------
from core.paths import BASE_DIR, DATA_DIR as DB_DIR, DB_PATH  # noqa: E402

# 线程锁（sqlite3 默认不允许跨线程共享连接）
_local = threading.local()


def _ensure_dirs() -> None:
    os.makedirs(DB_DIR, exist_ok=True)


def get_conn() -> sqlite3.Connection:
    """获取当前线程的 SQLite 连接（线程隔离）。"""
    if not hasattr(_local, "conn") or _local.conn is None:
        _ensure_dirs()
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return _local.conn


def init_db() -> None:
    """建表（幂等，重复调用安全）。"""
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS shops (
            shop_id     TEXT PRIMARY KEY,
            shop_name   TEXT NOT NULL DEFAULT '',
            marketplace TEXT NOT NULL DEFAULT 'A1VC38T7YXB528',
            last_product_sync  TEXT,
            last_search_sync   TEXT,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS products (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id         TEXT NOT NULL,
            shop_name       TEXT NOT NULL DEFAULT '',
            item_type       TEXT,
            parent_asin     TEXT,
            asin            TEXT,
            msku            TEXT,
            sku             TEXT,
            parent_sku      TEXT,
            raw_title       TEXT,
            clean_zh_name   TEXT,
            price           REAL DEFAULT 0,
            image_url       TEXT,
            online_status   TEXT,
            created_at      TEXT NOT NULL,
            UNIQUE(shop_id, msku, asin)
        );

        CREATE INDEX IF NOT EXISTS idx_products_shop     ON products(shop_id);
        CREATE INDEX IF NOT EXISTS idx_products_parent   ON products(shop_id, parent_sku);

        CREATE TABLE IF NOT EXISTS ad_search_terms_daily (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            report_date     TEXT NOT NULL,
            shop_id         TEXT NOT NULL,
            shop_name       TEXT NOT NULL DEFAULT '',
            campaign_id     TEXT,
            campaign_name   TEXT,
            group_id        TEXT,
            group_name      TEXT,
            search_term     TEXT,
            match_type      TEXT,
            impressions     INTEGER DEFAULT 0,
            clicks          INTEGER DEFAULT 0,
            ctr             REAL,
            ad_cost         REAL DEFAULT 0,
            cpc             REAL,
            ad_orders       INTEGER DEFAULT 0,
            ad_sales        REAL DEFAULT 0,
            cvr             REAL,
            acos            REAL,
            roas            REAL,
            suggested_bid   REAL,
            asin            TEXT,
            msku            TEXT,
            created_at      TEXT NOT NULL,
            UNIQUE(report_date, shop_id, search_term, campaign_id, match_type)
        );

        CREATE INDEX IF NOT EXISTS idx_st_date_shop  ON ad_search_terms_daily(report_date, shop_id);
        CREATE INDEX IF NOT EXISTS idx_st_shop       ON ad_search_terms_daily(shop_id);
        CREATE INDEX IF NOT EXISTS idx_st_orders     ON ad_search_terms_daily(ad_orders);

        -- v2.2：赛狐「投放产生 ASIN」维度（searchQueryType=searchAsin）。
        -- 与搜索词维度分开存，避免污染 ad_search_terms_daily 的唯一键，
        -- 也不会影响自动广告模块原有的任何查询。
        CREATE TABLE IF NOT EXISTS ad_search_asin_daily (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            report_date     TEXT NOT NULL,
            shop_id         TEXT NOT NULL,
            shop_name       TEXT NOT NULL DEFAULT '',
            campaign_id     TEXT,
            campaign_name   TEXT,
            group_id        TEXT,
            group_name      TEXT,
            target_asin     TEXT,          -- 投放产生的商品页 ASIN（接口 query）
            targeting_expression TEXT,    -- substitutes / asin="B0..." / close-match
            targeting_type  TEXT,          -- spTargeting / spQuery
            match_type      TEXT,
            impressions     INTEGER DEFAULT 0,
            clicks          INTEGER DEFAULT 0,
            ctr             REAL,
            ad_cost         REAL DEFAULT 0,
            cpc             REAL,
            ad_orders       INTEGER DEFAULT 0,
            ad_sales        REAL DEFAULT 0,
            cvr             REAL,
            acos            REAL,
            roas            REAL,
            suggested_bid   REAL,
            price           REAL,          -- 竞品价（ASIN 维度特有）
            asin_rating     REAL,
            rating_count    INTEGER,
            asin            TEXT,          -- 自家广告商品 ASIN
            msku            TEXT,
            created_at      TEXT NOT NULL,
            UNIQUE(report_date, shop_id, target_asin, campaign_id, targeting_expression)
        );

        CREATE INDEX IF NOT EXISTS idx_sa_date_shop ON ad_search_asin_daily(report_date, shop_id);
        CREATE INDEX IF NOT EXISTS idx_sa_orders    ON ad_search_asin_daily(ad_orders);
        """
    )
    # v2.4：多赛狐账号。店铺归属于哪个账号，用于前端分组和同步范围校验。
    _ensure_column(conn, "shops", "account_id", "TEXT NOT NULL DEFAULT ''")
    # v2.8：店铺自定义名。赛狐原名往往带 -JP 后缀且顺序杂乱，
    # 允许人工起名，所有下拉框按这个名字排序显示。
    _ensure_column(conn, "shops", "alias", "TEXT NOT NULL DEFAULT ''")
    conn.commit()


def _ensure_column(
    conn: sqlite3.Connection, table: str, column: str, ddl: str
) -> None:
    """幂等加列（老库升级用，SQLite 不支持 ADD COLUMN IF NOT EXISTS）。"""
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


# ---------------------------------------------------------------------------
# shops 表操作
# ---------------------------------------------------------------------------
def upsert_shop(
    shop_id: str,
    shop_name: str = "",
    marketplace: str = "A1VC38T7YXB528",
    account_id: str = "",
) -> None:
    """插入或更新店铺记录。

    ``account_id`` 为空时保留原有归属，避免单店同步把账号信息抹掉。
    """
    conn = get_conn()
    now = datetime.now().isoformat()
    conn.execute(
        """
        INSERT INTO shops (shop_id, shop_name, marketplace, account_id, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(shop_id) DO UPDATE SET
            shop_name   = excluded.shop_name,
            marketplace = excluded.marketplace,
            account_id  = CASE
                WHEN excluded.account_id != '' THEN excluded.account_id
                ELSE shops.account_id
            END
        """,
        (shop_id, shop_name, marketplace, account_id, now),
    )
    conn.commit()


def update_shop_sync_time(shop_id: str, sync_type: str = "product") -> None:
    """更新店铺的最后同步时间。"""
    conn = get_conn()
    now = datetime.now().isoformat()
    col = "last_product_sync" if sync_type == "product" else "last_search_sync"
    conn.execute(f"UPDATE shops SET {col} = ? WHERE shop_id = ?", (now, shop_id))
    conn.commit()


def list_shops(account_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """返回已注册的店铺；给了 ``account_id`` 只返回该账号名下的。

    排序：优先按人工起的名字（``alias``），没起名的按赛狐原名。
    SQLite 对中文按 UTF-8 字节排序，前端还会用 localeCompare 再排一次，
    这里只保证「同一批数据每次返回顺序稳定」。
    """
    conn = get_conn()
    order = "ORDER BY COALESCE(NULLIF(alias, ''), shop_name), shop_id"
    if account_id:
        rows = conn.execute(
            f"SELECT * FROM shops WHERE account_id = ? {order}",
            (account_id,),
        ).fetchall()
    else:
        rows = conn.execute(f"SELECT * FROM shops {order}").fetchall()
    return [dict(r) for r in rows]


def set_shop_alias(shop_id: str, alias: str) -> None:
    """给店铺设置自定义名；传空串表示恢复赛狐原名。"""
    conn = get_conn()
    conn.execute(
        "UPDATE shops SET alias = ? WHERE shop_id = ?",
        ((alias or "").strip(), shop_id),
    )
    conn.commit()


def get_shop(shop_id: str) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM shops WHERE shop_id = ?", (shop_id,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# products 表操作
# ---------------------------------------------------------------------------
def product_exists(shop_id: str, msku: str, asin: str) -> bool:
    """检查商品是否已存在于本地数据库。"""
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM products WHERE shop_id=? AND msku=? AND asin=? LIMIT 1",
        (shop_id, msku, asin),
    ).fetchone()
    return row is not None


def insert_product(
    shop_id: str,
    shop_name: str,
    item_type: str = "",
    parent_asin: str = "",
    asin: str = "",
    msku: str = "",
    sku: str = "",
    parent_sku: str = "",
    raw_title: str = "",
    clean_zh_name: str = "",
    price: float = 0.0,
    image_url: str = "",
    online_status: str = "",
) -> bool:
    """插入商品（增量去重：已存在则跳过，返回 False）。

    关键：不覆盖用户已微调过的中文品名。只有全新商品才入库。
    """
    if product_exists(shop_id, msku, asin):
        return False

    conn = get_conn()
    now = datetime.now().isoformat()
    conn.execute(
        """
        INSERT OR IGNORE INTO products
            (shop_id, shop_name, item_type, parent_asin, asin, msku, sku,
             parent_sku, raw_title, clean_zh_name, price, image_url,
             online_status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            shop_id, shop_name, item_type, parent_asin, asin, msku, sku,
            parent_sku, raw_title, clean_zh_name, price, image_url,
            online_status, now,
        ),
    )
    conn.commit()
    return True


def get_products_by_shop(shop_id: str) -> List[Dict[str, Any]]:
    """获取指定店铺的全部商品。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM products WHERE shop_id=? ORDER BY parent_sku, asin",
        (shop_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_products_grouped_by_parent(shop_id: str) -> List[Dict[str, Any]]:
    """按父 SKU 聚合商品（父体 + 子体列表），供自动广告模块使用。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM products WHERE shop_id=? ORDER BY parent_sku, asin",
        (shop_id,),
    ).fetchall()

    groups: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        r = dict(r)
        pkey = r.get("parent_sku") or r.get("asin") or r.get("msku", "")
        if pkey not in groups:
            groups[pkey] = {
                "parent_sku": pkey,
                "product_name": r.get("clean_zh_name") or r.get("raw_title") or "商品",
                "raw_name": r.get("raw_title") or "",
                "image_url": r.get("image_url") or "",
                "children": [],
                "prices": [],
            }
        child = {
            "msku": r.get("msku") or r.get("sku") or "",
            "asin": r.get("asin") or "",
            "price": r.get("price") or 0.0,
        }
        groups[pkey]["children"].append(child)
        if child["price"] > 0:
            groups[pkey]["prices"].append(child["price"])
        # 取更干净的品名
        raw = r.get("raw_title") or ""
        if raw and (not groups[pkey]["raw_name"] or len(raw) < len(groups[pkey]["raw_name"])):
            groups[pkey]["raw_name"] = raw

    return list(groups.values())


def get_parent_sku_map(shop_id: str) -> Dict[str, List[str]]:
    """返回 ``{父SKU: [子体 MSKU, ...]}``。

    用途：判断「这个父体是否已经在广告里」——广告商品行给的是 MSKU，
    得先知道每个 MSKU 归属哪个父体。
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT parent_sku, msku, sku FROM products WHERE shop_id=?", (shop_id,)
    ).fetchall()
    out: Dict[str, List[str]] = {}
    for r in rows:
        parent = str(r["parent_sku"] or "").strip()
        if not parent:
            continue
        bucket = out.setdefault(parent, [])
        for val in (r["msku"], r["sku"]):
            sku = str(val or "").strip()
            if sku and sku not in bucket:
                bucket.append(sku)
    return out


def count_products(shop_id: str) -> int:
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM products WHERE shop_id=?", (shop_id,)
    ).fetchone()
    return row["cnt"] if row else 0


# ---------------------------------------------------------------------------
# ad_search_terms_daily 表操作
# ---------------------------------------------------------------------------
def insert_search_term(row: Dict[str, Any]) -> bool:
    """写入一条搜索词数据。

    同「日期+店铺+搜索词+活动+匹配类型」已存在时**更新指标**（赛狐会回溯修正
    历史报表，重新同步必须能覆盖旧值），否则插入。返回是否实际写入了行，
    供同步结果给出真实的入库条数。
    """
    conn = get_conn()
    now = datetime.now().isoformat()

    # 数值安全转换
    def _to_int(v: Any, default: int = 0) -> int:
        try:
            return int(float(v)) if v is not None else default
        except (TypeError, ValueError):
            return default

    def _to_float(v: Any, default: float = 0.0) -> float:
        try:
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    cur = conn.execute(
        """
        INSERT INTO ad_search_terms_daily
            (report_date, shop_id, shop_name, campaign_id, campaign_name,
             group_id, group_name, search_term, match_type,
             impressions, clicks, ctr, ad_cost, cpc,
             ad_orders, ad_sales, cvr, acos, roas, suggested_bid,
             asin, msku, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(report_date, shop_id, search_term, campaign_id, match_type)
        DO UPDATE SET
            shop_name=excluded.shop_name, campaign_name=excluded.campaign_name,
            group_id=excluded.group_id, group_name=excluded.group_name,
            impressions=excluded.impressions, clicks=excluded.clicks, ctr=excluded.ctr,
            ad_cost=excluded.ad_cost, cpc=excluded.cpc,
            ad_orders=excluded.ad_orders, ad_sales=excluded.ad_sales,
            cvr=excluded.cvr, acos=excluded.acos, roas=excluded.roas,
            suggested_bid=excluded.suggested_bid,
            asin=excluded.asin, msku=excluded.msku, created_at=excluded.created_at
        """,
        (
            row.get("report_date", ""),
            row.get("shop_id", ""),
            row.get("shop_name", ""),
            row.get("campaign_id", ""),
            row.get("campaign_name", ""),
            row.get("group_id", ""),
            row.get("group_name", ""),
            row.get("search_term", ""),
            row.get("match_type", ""),
            _to_int(row.get("impressions")),
            _to_int(row.get("clicks")),
            _to_float(row.get("ctr")),
            _to_float(row.get("ad_cost")),
            _to_float(row.get("cpc")),
            _to_int(row.get("ad_orders")),
            _to_float(row.get("ad_sales")),
            _to_float(row.get("cvr")),
            _to_float(row.get("acos")),
            _to_float(row.get("roas")),
            _to_float(row.get("suggested_bid")),
            row.get("asin", ""),
            row.get("msku", ""),
            now,
        ),
    )
    conn.commit()
    return cur.rowcount > 0


def insert_search_terms_batch(rows: List[Dict[str, Any]]) -> int:
    """批量写入搜索词数据，返回实际写入/更新的条数。"""
    count = 0
    for r in rows:
        if insert_search_term(r):
            count += 1
    return count


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def insert_search_asin(row: Dict[str, Any]) -> bool:
    """插入一条「投放产生 ASIN」维度数据（INSERT OR IGNORE 去重）。"""
    conn = get_conn()
    now = datetime.now().isoformat()
    cur = conn.execute(
        """
        INSERT INTO ad_search_asin_daily
            (report_date, shop_id, shop_name, campaign_id, campaign_name,
             group_id, group_name, target_asin, targeting_expression,
             targeting_type, match_type, impressions, clicks, ctr, ad_cost, cpc,
             ad_orders, ad_sales, cvr, acos, roas, suggested_bid,
             price, asin_rating, rating_count, asin, msku, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(report_date, shop_id, target_asin, campaign_id, targeting_expression)
        DO UPDATE SET
            shop_name=excluded.shop_name, campaign_name=excluded.campaign_name,
            group_id=excluded.group_id, group_name=excluded.group_name,
            targeting_type=excluded.targeting_type, match_type=excluded.match_type,
            impressions=excluded.impressions, clicks=excluded.clicks, ctr=excluded.ctr,
            ad_cost=excluded.ad_cost, cpc=excluded.cpc,
            ad_orders=excluded.ad_orders, ad_sales=excluded.ad_sales,
            cvr=excluded.cvr, acos=excluded.acos, roas=excluded.roas,
            suggested_bid=excluded.suggested_bid, price=excluded.price,
            asin_rating=excluded.asin_rating, rating_count=excluded.rating_count,
            asin=excluded.asin, msku=excluded.msku, created_at=excluded.created_at
        """,
        (
            row.get("report_date", ""), row.get("shop_id", ""), row.get("shop_name", ""),
            row.get("campaign_id", ""), row.get("campaign_name", ""),
            row.get("group_id", ""), row.get("group_name", ""),
            str(row.get("target_asin") or "").strip().upper(),
            row.get("targeting_expression", ""), row.get("targeting_type", ""),
            row.get("match_type", ""),
            int(_num(row.get("impressions"))), int(_num(row.get("clicks"))),
            _num(row.get("ctr")), _num(row.get("ad_cost")), _num(row.get("cpc")),
            int(_num(row.get("ad_orders"))), _num(row.get("ad_sales")),
            _num(row.get("cvr")), _num(row.get("acos")), _num(row.get("roas")),
            _num(row.get("suggested_bid")), _num(row.get("price")),
            _num(row.get("asin_rating")), int(_num(row.get("rating_count"))),
            row.get("asin", ""), row.get("msku", ""), now,
        ),
    )
    conn.commit()
    return cur.rowcount > 0


def insert_search_asins_batch(rows: List[Dict[str, Any]]) -> int:
    return sum(1 for r in rows if insert_search_asin(r))


def _attach_order_dates(
    rows: List[Dict[str, Any]],
    table: str,
    key_col: str,
    shop_id: Optional[str],
    start_date: Optional[str],
    end_date: Optional[str],
) -> List[Dict[str, Any]]:
    """给每行补上「出单时间」：同店铺+同词/ASIN+同活动在查询区间内所有出单日期。

    赛狐的区间报表把 10 天出单汇总成一行，本地库是按天存的，
    没有这个字段就无法看出「到底哪几天出的单」，也无法和赛狐核对。
    """
    if not rows:
        return rows
    conn = get_conn()
    sql = (
        f"SELECT shop_id, {key_col} AS k, campaign_id, "
        f"GROUP_CONCAT(DISTINCT report_date) AS ds "
        f"FROM {table} WHERE ad_orders >= 1"
    )
    params: List[Any] = []
    if shop_id:
        sql += " AND shop_id = ?"
        params.append(shop_id)
    if start_date:
        sql += " AND report_date >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND report_date <= ?"
        params.append(end_date)
    sql += " GROUP BY shop_id, k, campaign_id"
    mapping = {
        (str(x["shop_id"]), str(x["k"] or ""), str(x["campaign_id"] or "")): (x["ds"] or "")
        for x in conn.execute(sql, params).fetchall()
    }
    for r in rows:
        ds = mapping.get(
            (str(r.get("shop_id") or ""), str(r.get("search_term") or ""),
             str(r.get("campaign_id") or "")), ""
        )
        dates = sorted(d for d in ds.split(",") if d)
        r["order_dates"] = "、".join(d[5:] for d in dates)   # 11-07、11-09
        r["order_date"] = dates[-1] if dates else (r.get("report_date") or "")
    return rows


# ---------------------------------------------------------------------------
# 区间聚合查询（v2.3）
#
# 赛狐的「广告搜索词 / 投放产生 ASIN」页面给的是**区间汇总一行**，而本地库按天存。
# 直接把按天明细丢给前端，同一词会散成多行，用户拿去和赛狐核对时必然对不上。
# 这里按「店铺 + 活动 + 广告组 + 词/ASIN + 匹配类型」在所选日期区间内求和，
# 并用 HAVING SUM(ad_orders) >= 1 复现赛狐「有成交」的区间口径。
# ---------------------------------------------------------------------------
def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _agg_filters(
    shop_id: Optional[str],
    start_date: Optional[str],
    end_date: Optional[str],
    keyword: str,
    like_cols: List[str],
):
    clauses: List[str] = []
    params: List[Any] = []
    if shop_id:
        clauses.append("shop_id = ?")
        params.append(shop_id)
    if start_date:
        clauses.append("report_date >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("report_date <= ?")
        params.append(end_date)
    if keyword:
        clauses.append("(" + " OR ".join(f"{c} LIKE ?" for c in like_cols) + ")")
        params.extend([f"%{keyword}%"] * len(like_cols))
    return (" AND ".join(clauses) or "1=1"), params


def _agg_finish(rows: List[Dict[str, Any]]) -> None:
    """按赛狐口径重算聚合后的 CPC / CVR / ACOS / ROAS。"""
    for r in rows:
        clicks = _f(r.get("clicks"))
        cost = _f(r.get("ad_cost"))
        orders = _f(r.get("ad_orders"))
        sales = _f(r.get("ad_sales"))
        r["cpc"] = round(cost / clicks, 2) if clicks else 0.0
        r["cvr"] = round(orders / clicks * 100, 2) if clicks else 0.0
        r["acos"] = round(cost / sales * 100, 2) if sales else 0.0
        r["roas"] = round(sales / cost, 2) if cost else 0.0


def _agg_sort(
    rows: List[Dict[str, Any]], order_by: str, order_desc: bool, limit: int
) -> List[Dict[str, Any]]:
    text_keys = {"search_term", "report_date", "campaign_name", "shop_name"}
    numeric_keys = {
        "ad_orders", "ad_cost", "ad_sales", "clicks", "impressions",
        "acos", "cvr", "cpc", "roas", "suggested_bid", "price",
    }
    if order_by in text_keys:
        rows.sort(key=lambda r: str(r.get(order_by) or ""), reverse=order_desc)
    else:
        key = order_by if order_by in numeric_keys else "ad_orders"
        rows.sort(key=lambda r: _f(r.get(key)), reverse=order_desc)
    return rows[:max(0, int(limit or 0))]


def _agg_dimension(dimension: str) -> Dict[str, str]:
    if dimension == "asin":
        return {
            "table": "ad_search_asin_daily",
            "keys": "target_asin AS search_term, target_asin AS target_asin",
            "group": "shop_id, campaign_id, group_id, target_asin, targeting_expression",
            "extra": (
                "MAX(targeting_expression) AS targeting_expression, "
                "MAX(targeting_type) AS targeting_type, "
                "MAX(match_type) AS match_type, MAX(price) AS price, "
                "MAX(asin) AS asin, MAX(msku) AS msku, "
                "MAX(suggested_bid) AS suggested_bid"
            ),
            "like": "target_asin, campaign_name, msku",
            "attach_key": "target_asin",
        }
    return {
        "table": "ad_search_terms_daily",
        "keys": "search_term",
        "group": "shop_id, campaign_id, group_id, search_term, match_type",
        "extra": (
            "MAX(match_type) AS match_type, MAX(asin) AS asin, "
            "MAX(msku) AS msku, MAX(suggested_bid) AS suggested_bid, "
            "MAX(cpc) AS original_cpc"
        ),
        "like": "search_term, campaign_name, msku",
        "attach_key": "search_term",
    }


def _agg_query(
    dimension: str,
    shop_id: Optional[str],
    start_date: Optional[str],
    end_date: Optional[str],
    only_orders: bool,
    keyword: str,
    order_by: str,
    order_desc: bool,
    limit: int,
) -> List[Dict[str, Any]]:
    cfg = _agg_dimension(dimension)
    conn = get_conn()
    where, params = _agg_filters(
        shop_id, start_date, end_date, keyword, cfg["like"].split(", ")
    )
    sql = (
        "SELECT shop_id, MAX(shop_name) AS shop_name, "
        "campaign_id, MAX(campaign_name) AS campaign_name, "
        "group_id, MAX(group_name) AS group_name, "
        f"{cfg['keys']}, {cfg['extra']}, "
        "SUM(impressions) AS impressions, SUM(clicks) AS clicks, "
        "SUM(ad_cost) AS ad_cost, SUM(ad_orders) AS ad_orders, "
        "SUM(ad_sales) AS ad_sales, MIN(report_date) AS report_date, "
        f"'{dimension}' AS dimension "
        f"FROM {cfg['table']} WHERE {where} GROUP BY {cfg['group']}"
    )
    if only_orders:
        # 区间累计有成交，而不是「某一天有成交」——与赛狐「有成交」筛选项一致
        sql += " HAVING SUM(ad_orders) >= 1"
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    _agg_finish(rows)
    rows = _agg_sort(rows, order_by, order_desc, limit)
    return _attach_order_dates(
        rows, cfg["table"], cfg["attach_key"], shop_id, start_date, end_date
    )


def _agg_count(
    dimension: str,
    shop_id: Optional[str],
    start_date: Optional[str],
    end_date: Optional[str],
    only_orders: bool,
) -> int:
    cfg = _agg_dimension(dimension)
    conn = get_conn()
    where, params = _agg_filters(
        shop_id, start_date, end_date, "", cfg["like"].split(", ")
    )
    sql = (
        f"SELECT COUNT(*) AS cnt FROM (SELECT 1 FROM {cfg['table']} "
        f"WHERE {where} GROUP BY {cfg['group']}"
    )
    if only_orders:
        sql += " HAVING SUM(ad_orders) >= 1"
    sql += ")"
    row = conn.execute(sql, params).fetchone()
    return int(row["cnt"]) if row else 0


def query_search_asins(
    shop_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    only_orders: bool = True,
    keyword: str = "",
    order_by: str = "ad_orders",
    order_desc: bool = True,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """查询「投放产生 ASIN」维度，输出字段与搜索词维度保持一致，前端可复用同一张表。"""
    conn = get_conn()
    sql = (
        "SELECT id, report_date, shop_id, shop_name, campaign_id, campaign_name, "
        "target_asin AS search_term, target_asin AS target_asin, "
        "targeting_expression, targeting_type, match_type, "
        "impressions, clicks, ad_cost, cpc, ad_orders, ad_sales, acos, cvr, "
        "suggested_bid, price, asin, msku, 'asin' AS dimension "
        "FROM ad_search_asin_daily WHERE 1=1"
    )
    params: List[Any] = []
    if shop_id:
        sql += " AND shop_id = ?"
        params.append(shop_id)
    if start_date:
        sql += " AND report_date >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND report_date <= ?"
        params.append(end_date)
    if only_orders:
        sql += " AND ad_orders >= 1"
    if keyword:
        sql += " AND (target_asin LIKE ? OR campaign_name LIKE ? OR msku LIKE ?)"
        kw = f"%{keyword}%"
        params.extend([kw, kw, kw])
    allowed = {
        "ad_orders", "ad_cost", "ad_sales", "clicks", "impressions",
        "acos", "cvr", "cpc", "suggested_bid", "search_term", "report_date", "price",
    }
    if order_by not in allowed:
        order_by = "ad_orders"
    sql += f" ORDER BY {order_by} {'DESC' if order_desc else 'ASC'} LIMIT {limit}"
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    return _attach_order_dates(
        rows, "ad_search_asin_daily", "target_asin", shop_id, start_date, end_date
    )


def count_search_asins(
    shop_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    only_orders: bool = True,
) -> int:
    conn = get_conn()
    sql = "SELECT COUNT(*) AS cnt FROM ad_search_asin_daily WHERE 1=1"
    params: List[Any] = []
    if shop_id:
        sql += " AND shop_id = ?"
        params.append(shop_id)
    if start_date:
        sql += " AND report_date >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND report_date <= ?"
        params.append(end_date)
    if only_orders:
        sql += " AND ad_orders >= 1"
    return conn.execute(sql, params).fetchone()["cnt"]


def query_search_terms(
    shop_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    only_orders: bool = True,
    keyword: str = "",
    order_by: str = "ad_orders",
    order_desc: bool = True,
    limit: int = 500,
    dimension: str = "keyword",
    aggregate: bool = False,
) -> List[Dict[str, Any]]:
    """多维筛选搜索词数据。

    - ``dimension='asin'`` 改查「投放产生 ASIN」维度；
    - ``aggregate=True`` 把查询区间内的按天明细汇总成一行（对齐赛狐区间报表）。
    """
    if aggregate:
        return _agg_query(
            dimension, shop_id, start_date, end_date, only_orders,
            keyword, order_by, order_desc, limit,
        )
    if dimension == "asin":
        return query_search_asins(
            shop_id, start_date, end_date, only_orders, keyword, order_by, order_desc, limit
        )
    conn = get_conn()
    sql = "SELECT *, 'keyword' AS dimension FROM ad_search_terms_daily WHERE 1=1"
    params: List[Any] = []

    if shop_id:
        sql += " AND shop_id = ?"
        params.append(shop_id)
    if start_date:
        sql += " AND report_date >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND report_date <= ?"
        params.append(end_date)
    if only_orders:
        sql += " AND ad_orders >= 1"
    if keyword:
        sql += " AND (search_term LIKE ? OR campaign_name LIKE ? OR msku LIKE ?)"
        kw = f"%{keyword}%"
        params.extend([kw, kw, kw])

    # 安全排序
    allowed_sort = {
        "ad_orders", "ad_cost", "ad_sales", "clicks", "impressions",
        "acos", "cvr", "cpc", "suggested_bid", "search_term", "report_date",
    }
    if order_by not in allowed_sort:
        order_by = "ad_orders"
    sql += f" ORDER BY {order_by} {'DESC' if order_desc else 'ASC'}"
    sql += f" LIMIT {limit}"

    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    return _attach_order_dates(
        rows, "ad_search_terms_daily", "search_term", shop_id, start_date, end_date
    )


def count_rows_for_date(shop_id: str, report_date: str) -> Dict[str, int]:
    """某店铺某一天已入库的行数（关键词 / 投放 ASIN 两个维度）。

    用途：手动广告页「查询并同步」时只补本地没有的日期，已同步过的直接跳过，
    避免每次查询都把整个区间重新拉一遍。
    """
    conn = get_conn()
    kw = conn.execute(
        "SELECT COUNT(*) AS c FROM ad_search_terms_daily"
        " WHERE shop_id = ? AND report_date = ?",
        (shop_id, report_date),
    ).fetchone()
    asin = conn.execute(
        "SELECT COUNT(*) AS c FROM ad_search_asin_daily"
        " WHERE shop_id = ? AND report_date = ?",
        (shop_id, report_date),
    ).fetchone()
    kw_n = kw["c"] if kw else 0
    asin_n = asin["c"] if asin else 0
    return {"keyword": kw_n, "asin": asin_n, "total": kw_n + asin_n}


def count_search_terms(
    shop_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    only_orders: bool = True,
    dimension: str = "keyword",
    aggregate: bool = False,
) -> int:
    """统计搜索词总数（aggregate=True 时统计聚合后的行数）。"""
    if aggregate:
        return _agg_count(dimension, shop_id, start_date, end_date, only_orders)
    if dimension == "asin":
        return count_search_asins(shop_id, start_date, end_date, only_orders)
    conn = get_conn()
    sql = "SELECT COUNT(*) as cnt FROM ad_search_terms_daily WHERE 1=1"
    params: List[Any] = []
    if shop_id:
        sql += " AND shop_id = ?"
        params.append(shop_id)
    if start_date:
        sql += " AND report_date >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND report_date <= ?"
        params.append(end_date)
    if only_orders:
        sql += " AND ad_orders >= 1"
    row = conn.execute(sql, params).fetchone()
    return row["cnt"] if row else 0


# ---------------------------------------------------------------------------
# 统计信息
# ---------------------------------------------------------------------------
def db_stats() -> Dict[str, Any]:
    """数据库总览统计。"""
    conn = get_conn()
    shops_cnt = conn.execute("SELECT COUNT(*) as c FROM shops").fetchone()["c"]
    products_cnt = conn.execute("SELECT COUNT(*) as c FROM products").fetchone()["c"]
    st_cnt = conn.execute("SELECT COUNT(*) as c FROM ad_search_terms_daily").fetchone()["c"]
    st_orders = conn.execute(
        "SELECT COUNT(*) as c FROM ad_search_terms_daily WHERE ad_orders >= 1"
    ).fetchone()["c"]
    return {
        "shops": shops_cnt,
        "products": products_cnt,
        "search_terms_total": st_cnt,
        "search_terms_with_orders": st_orders,
        "db_path": DB_PATH,
    }


# 启动时自动建表
try:
    init_db()
except Exception:
    # 数据库可能尚未就绪（如 data 目录不存在），延迟到首次调用时建表
    pass
