"""SQLite 本地数据持久化层。

三张核心表：
1. ``shops`` — 店铺注册表（shop_id / shop_name / 站点 / 最后同步时间）
2. ``products`` — 商品库（服务于自动广告模块），(shop_id, msku, asin) 联合唯一索引，
   增量去重：已存在的旧商品直接跳过，仅入库新上架商品。
3. ``ad_search_terms_daily`` — 每日搜索词报告（服务于手动广告模块），
   按日存储搜索词维度的曝光/点击/花费/出单/ACoS/CVR/建议竞价等全量指标。

设计原则
--------
- 全部用标准库 ``sqlite3``，无需额外依赖（SQLAlchemy 留作可选）。
- 线程安全：每次操作 ``with get_conn()`` 自动 commit / close。
- ``INSERT OR IGNORE`` 实现增量去重，不覆盖用户已微调的中文品名。
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
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DB_DIR, "local_store.db")

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
        """
    )
    conn.commit()


# ---------------------------------------------------------------------------
# shops 表操作
# ---------------------------------------------------------------------------
def upsert_shop(shop_id: str, shop_name: str = "", marketplace: str = "A1VC38T7YXB528") -> None:
    """插入或更新店铺记录。"""
    conn = get_conn()
    now = datetime.now().isoformat()
    conn.execute(
        """
        INSERT INTO shops (shop_id, shop_name, marketplace, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(shop_id) DO UPDATE SET
            shop_name   = excluded.shop_name,
            marketplace = excluded.marketplace
        """,
        (shop_id, shop_name, marketplace, now),
    )
    conn.commit()


def update_shop_sync_time(shop_id: str, sync_type: str = "product") -> None:
    """更新店铺的最后同步时间。"""
    conn = get_conn()
    now = datetime.now().isoformat()
    col = "last_product_sync" if sync_type == "product" else "last_search_sync"
    conn.execute(f"UPDATE shops SET {col} = ? WHERE shop_id = ?", (now, shop_id))
    conn.commit()


def list_shops() -> List[Dict[str, Any]]:
    """返回所有已注册的店铺。"""
    conn = get_conn()
    rows = conn.execute("SELECT * FROM shops ORDER BY shop_id").fetchall()
    return [dict(r) for r in rows]


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
    """插入一条搜索词数据（去重：同日期+店铺+搜索词+活动+匹配类型只存一条）。"""
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

    conn.execute(
        """
        INSERT OR IGNORE INTO ad_search_terms_daily
            (report_date, shop_id, shop_name, campaign_id, campaign_name,
             group_id, group_name, search_term, match_type,
             impressions, clicks, ctr, ad_cost, cpc,
             ad_orders, ad_sales, cvr, acos, roas, suggested_bid,
             asin, msku, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    return True


def insert_search_terms_batch(rows: List[Dict[str, Any]]) -> int:
    """批量插入搜索词数据，返回成功插入的条数。"""
    count = 0
    for r in rows:
        if insert_search_term(r):
            count += 1
    return count


def query_search_terms(
    shop_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    only_orders: bool = True,
    keyword: str = "",
    order_by: str = "ad_orders",
    order_desc: bool = True,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """多维筛选搜索词数据。"""
    conn = get_conn()
    sql = "SELECT * FROM ad_search_terms_daily WHERE 1=1"
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

    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def count_search_terms(
    shop_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    only_orders: bool = True,
) -> int:
    """统计搜索词总数。"""
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
