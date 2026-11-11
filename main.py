"""Amazon Bulk Operations Ads Generator —— FastAPI 主入口（v2.0 全自动数据同步与双模块系统）。

一键运行：``python main.py``
启动后自动在浏览器打开 http://127.0.0.1:8000

v2.0 升级（重大）
--------------------------------------------------------------------
1. **赛狐 API 接入**：不再需要手动下载 Excel，通过赛狐底层 API 自动拉取商品和搜索词数据。
2. **本地 SQLite 持久化**：商品库和每日搜索词报告持久化存储，支持增量去重。
3. **APScheduler 定时任务**：商品每周一 03:00 增量同步，搜索词每日 02:00 自动拉取。
4. **双模块驱动**：
   - **自动广告模块**：从本地数据库读取商品，保留 v1~v10 全部能力。
   - **手动广告模块**（全新）：搜索词出单分析工作台 + 一键创建手动关键词广告。
5. **Cookie 管理**：支持 Playwright CDP 自动抓取 + 手动粘贴两种方式。

保留 v1~v10 全部修复：
- v10 临时 ID 非纯数字校验（``c`` 前缀）
- v9 表头与官方 31 列逐字对齐
- v8 实体层级/投放表达式受控值修正
- v7 离线名词词库 + 动态重匹配
"""

from __future__ import annotations

import datetime
import json
import os
import threading
import time
import webbrowser
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.budget_engine import build_shop_payload
from core.bulk_generator import generate_bulk_excel, generate_manual_keyword_bulk
from core.parser import parse_sellfox_excel_multi_shop
from core.translator import noun_matcher
from core import database as db
from core import sellfox_client as sf
from core import sync_service
from core import scheduler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = FastAPI(
    title="Amazon Bulk Ads Generator v2.0",
    description="全自动数据同步与双模块（自动广告 + 手动广告）系统",
    version="2.0.0",
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class ExportRequest(BaseModel):
    products: List[Dict[str, Any]]
    settings: Dict[str, Any] = {}


class ManualAdExportRequest(BaseModel):
    """手动广告 Bulk 导出请求。"""
    campaigns: List[Dict[str, Any]]
    settings: Dict[str, Any] = {}


class ManualAdCreateRequest(BaseModel):
    """手动广告接口直推赛狐请求。"""
    shop_id: str
    campaigns: List[Dict[str, Any]]
    settings: Dict[str, Any] = {}


class DictEntry(BaseModel):
    ja: str
    zh: str


class RematchItem(BaseModel):
    key: str = ""
    parent_sku: str = ""
    raw_name: str = ""


class RematchRequest(BaseModel):
    items: List[RematchItem] = []


class CookieUpdateRequest(BaseModel):
    """Cookie 更新请求。"""
    cookie_str: str
    merge: bool = True   # 与已有 Cookie 合并，避免只粘贴一部分时冲掉登录态
    account_id: str = "" # 空 = 按登录态指纹自动归属（同一账号只更新不新增）
    label: str = ""      # 账号备注名，如「账号A / 大成组」


class AccountCreateRequest(BaseModel):
    """新增赛狐账号（粘贴该账号的 Cookie）。"""
    cookie_str: str
    label: str = ""
    merge: bool = True


class AccountRenameRequest(BaseModel):
    """重命名账号备注。"""
    label: str


class ShopRegisterRequest(BaseModel):
    """注册店铺请求。"""
    shop_id: str
    shop_name: str = ""
    marketplace: str = "A1VC38T7YXB528"


class SyncTriggerRequest(BaseModel):
    """手动触发同步请求。"""
    shop_id: str = ""
    target_date: str = ""
    days: int = 1        # 搜索词回补天数（1 = 仅昨天）
    account_id: str = "" # 空 = 当前激活账号


class SyncAllAccountsRequest(BaseModel):
    """全量同步所有赛狐账号。"""
    kind: str = "search"        # search=搜索词双维度 / product=商品 / both=两者
    target_date: str = ""
    days: int = 1
    account_ids: List[str] = []  # 空 = 全部账号


class SearchTermQueryRequest(BaseModel):
    """搜索词查询请求。"""
    shop_id: str = ""
    start_date: str = ""
    end_date: str = ""
    only_orders: bool = True
    keyword: str = ""
    order_by: str = "ad_orders"
    order_desc: bool = True
    limit: int = 500
    dimension: str = "keyword"   # keyword=用户搜索词 / asin=投放产生 ASIN
    aggregate: bool = True       # 按查询区间汇总成一行（对齐赛狐区间报表）


# ===========================================================================
# 页面与健康检查
# ===========================================================================
@app.get("/")
def read_root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "version": "2.0.0",
        "modules": ["auto_ads", "manual_ads", "cookie_mgmt", "scheduler"],
        "cookie_valid": sf.has_valid_cookies(),
        "dict_count": len(noun_matcher.dictionary),
        **db.db_stats(),
    }


# ===========================================================================
# v1.0 兼容接口：Excel 上传解析 + 自动广告 Bulk 导出
# ===========================================================================
@app.post("/api/analyze")
async def analyze_file(file: UploadFile = File(...)):
    """接收赛狐导出 Excel，按店铺分层返回商品聚合结果与预算建议（v1 兼容）。"""
    try:
        contents = await file.read()
        parsed = parse_sellfox_excel_multi_shop(contents)
        shops = build_shop_payload(parsed, base_budget=100.0)
        if not shops:
            raise ValueError("未从 Excel 中解析出任何有效的商品及店铺数据。")
        return {
            "shops": shops,
            "header_row": parsed.get("header_row", 0),
            "resolved_columns": parsed.get("resolved_columns", {}),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"解析失败：{exc}") from exc


@app.post("/api/generate")
def generate_file(payload: ExportRequest):
    """根据勾选商品与配置生成自动广告 Bulk Excel 并下载。"""
    buffer = generate_bulk_excel(payload.products, payload.settings)
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=amazon_bulk_ads_auto.xlsx"},
    )


# ===========================================================================
# v2.0 店铺管理
# ===========================================================================
@app.get("/api/shops")
def get_shops():
    """获取所有已注册店铺（带所属账号，便于前端按账号分组）。"""
    return {
        "shops": db.list_shops(),
        "accounts": sf.list_accounts(),
        "active_account": sf.active_account_id(),
    }


@app.post("/api/shops/register")
def register_shop(req: ShopRegisterRequest):
    """注册/更新一个店铺。"""
    db.upsert_shop(req.shop_id, req.shop_name, req.marketplace)
    return {"ok": True, "shop": db.get_shop(req.shop_id)}


@app.post("/api/shops/discover")
def discover_shops(account_id: str = ""):
    """自动发现指定账号（默认当前激活账号）下的全部店铺并写入本地库。

    赛狐没有独立的店铺列表接口，改为从广告活动接口返回行的
    ``shopId`` / ``shopName`` 反推；再用商品接口兜底补充。
    """
    if account_id:
        if not sf.get_account(account_id):
            raise HTTPException(status_code=404, detail="账号不存在")
        with sf.use_account(account_id):
            if not sf.has_valid_cookies():
                raise HTTPException(status_code=401, detail="该账号 Cookie 未配置或已失效")
            found = sync_service.discover_and_register_shops(account_id)
    else:
        if not sf.has_valid_cookies():
            raise HTTPException(status_code=401, detail="Cookie 未配置，请先导入赛狐登录态")
        found = sync_service.discover_and_register_shops()

    invalidate_shop_access_cache()
    return {
        "ok": True,
        "count": len(found),
        "discovered": found,
        "shops": db.list_shops(),
        "accounts": sf.list_accounts(),
    }


# ===========================================================================
# v2.0 Cookie 管理
# ===========================================================================
@app.get("/api/cookie/status")
def cookie_status():
    """检查 Cookie 状态（含多账号摘要）。"""
    accounts = sf.list_accounts()
    return {
        "valid": sf.has_valid_cookies(),
        "cookie_count": len(sf.load_cookies()),
        "active_account": sf.active_account_id(),
        "account_count": len(accounts),
        "accounts": accounts,
    }


# discover_shops() 要翻活动列表，实测一次 12~14 秒；前端首屏和每次切 Tab
# 都重新探测会太慢，这里做 5 分钟内存缓存。Cookie 一变更立即失效。
_SHOP_ACCESS_CACHE: Dict[str, Any] = {"ts": 0.0, "payload": None}
_SHOP_ACCESS_TTL = 300


def invalidate_shop_access_cache() -> None:
    """Cookie 或店铺清单变更后调用，避免账号提示停留在旧结果。"""
    _SHOP_ACCESS_CACHE["ts"] = 0.0
    _SHOP_ACCESS_CACHE["payload"] = None


@app.get("/api/cookie/shop-access")
def cookie_shop_access(force: bool = False):
    """探测当前 Cookie 实际能访问的店铺，并与本地已注册店铺对比。

    赛狐一个账号只对应一批店铺；如果用户把另一个账号的 Cookie 粘进来，
    同步会静默拉到完全不相干的数据（或报「没有店铺访问权限」）。
    这个接口让前端能直接把账号串号问题显示出来。
    """
    cached = _SHOP_ACCESS_CACHE["payload"]
    if (not force and cached
            and time.time() - _SHOP_ACCESS_CACHE["ts"] < _SHOP_ACCESS_TTL):
        return {**cached, "cached": True}

    today = datetime.date.today()
    try:
        found = sf.discover_shops(
            (today - datetime.timedelta(days=7)).strftime("%Y-%m-%d"),
            (today - datetime.timedelta(days=1)).strftime("%Y-%m-%d"),
        )
    except Exception as exc:
        # 失败结果不缓存，下次还能重试
        return {"ok": False, "error": str(exc), "accessible": [], "missing": []}
    accessible = [
        {"shop_id": str(x["shop_id"]), "shop_name": x["shop_name"]} for x in found
    ]
    ids = {x["shop_id"] for x in accessible}
    missing = [
        {"shop_id": str(s["shop_id"]), "shop_name": s["shop_name"]}
        for s in db.list_shops() if str(s["shop_id"]) not in ids
    ]
    payload = {
        "ok": True,
        "accessible": accessible,
        "missing": missing,
        "cookie_count": len(sf.load_cookies()),
    }
    _SHOP_ACCESS_CACHE["payload"] = payload
    _SHOP_ACCESS_CACHE["ts"] = time.time()
    return {**payload, "cached": False}


@app.post("/api/cookie/update")
def cookie_update(req: CookieUpdateRequest):
    """手动粘贴 Cookie 更新登录态。

    兼容纯 cookie 串 / 带 ``Cookie:`` 前缀 / 整段 Request Headers /
    Copy as cURL / document.cookie / JSON 对象 / JSON 数组。
    """
    parsed = sf.parse_cookie_input(req.cookie_str)
    if not parsed:
        raise HTTPException(
            status_code=400,
            detail="没有解析到任何 Cookie。请在开发者工具的「网络 → 标头 → 请求标头」里复制 cookie 那一行的值。",
        )
    info = sf.add_account(
        req.cookie_str, label=req.label,
        account_id=req.account_id, merge=req.merge,
    )
    if not info:
        raise HTTPException(status_code=400, detail="Cookie 解析失败，未能保存账号")
    invalidate_shop_access_cache()   # 换账号后必须重新探测
    return {
        "ok": True,
        "parsed": len(parsed),
        "cookie_count": info["cookie_count"],
        "has_login": info["has_login"],
        "valid": info["valid"],
        "account": info,
        "account_count": len(sf.list_accounts()),
    }


# ===========================================================================
# v2.4 多赛狐账号管理
# ===========================================================================
@app.get("/api/accounts")
def list_accounts():
    """列出全部赛狐账号（不含 Cookie 明文）。"""
    accounts = sf.list_accounts()
    return {
        "accounts": accounts,
        "active": sf.active_account_id(),
        "count": len(accounts),
    }


@app.post("/api/accounts")
def create_account(req: AccountCreateRequest):
    """新增一个赛狐账号。

    同一账号重复添加只会更新 Cookie，不会产生重复记录（按登录态指纹识别）。
    添加后立即探测该账号能访问的店铺，方便确认粘对了没有。
    """
    if not sf.parse_cookie_input(req.cookie_str):
        raise HTTPException(
            status_code=400,
            detail="没有解析到任何 Cookie。请复制赛狐页面「网络 → 请求标头 → cookie」的整行值。",
        )
    info = sf.add_account(
        req.cookie_str, label=req.label, merge=req.merge
    )
    if not info:
        raise HTTPException(status_code=400, detail="Cookie 保存失败")

    probe_error = ""
    try:
        with sf.use_account(info["id"]):
            found = sync_service.discover_and_register_shops(info["id"])
        info = next(
            (a for a in sf.list_accounts() if a["id"] == info["id"]), info
        )
        info["probe_shops"] = len(found)
    except Exception as exc:  # noqa: BLE001 - 探测失败不影响账号已保存
        probe_error = str(exc)

    invalidate_shop_access_cache()
    return {
        "ok": True,
        "account": info,
        "probe_error": probe_error,
        "accounts": sf.list_accounts(),
    }


@app.post("/api/accounts/{account_id}/activate")
def activate_account(account_id: str):
    """切换当前使用的账号（影响单店同步 / 手动查询 / 创建广告）。"""
    if not sf.set_active_account(account_id):
        raise HTTPException(status_code=404, detail="账号不存在")
    invalidate_shop_access_cache()
    return {"ok": True, "active": account_id}


@app.patch("/api/accounts/{account_id}")
def rename_account(account_id: str, req: AccountRenameRequest):
    """修改账号备注名。"""
    if not sf.rename_account(account_id, req.label):
        raise HTTPException(status_code=404, detail="账号不存在")
    return {"ok": True, "accounts": sf.list_accounts()}


@app.delete("/api/accounts/{account_id}")
def remove_account(account_id: str):
    """删除账号（只删本地登录态，不影响赛狐后台）。"""
    if not sf.delete_account(account_id):
        raise HTTPException(status_code=404, detail="账号不存在")
    invalidate_shop_access_cache()
    return {"ok": True, "accounts": sf.list_accounts()}


@app.post("/api/accounts/{account_id}/probe")
def probe_account(account_id: str):
    """重新探测某账号能访问的店铺（换绑店铺 / 权限变更后用）。"""
    if not sf.get_account(account_id):
        raise HTTPException(status_code=404, detail="账号不存在")
    try:
        with sf.use_account(account_id):
            found = sync_service.discover_and_register_shops(account_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"探测失败：{exc}") from exc
    invalidate_shop_access_cache()
    return {
        "ok": True,
        "count": len(found),
        "discovered": found,
        "accounts": sf.list_accounts(),
    }


@app.post("/api/cookie/grab")
def cookie_grab():
    """通过 Playwright CDP 自动从浏览器抓取 Cookie。

    前提：Chrome 带 --remote-debugging-port=9222 启动且已登录赛狐。
    """
    try:
        cookies = sf.grab_cookies_via_cdp()
        invalidate_shop_access_cache()
        return {"ok": True, "cookie_count": len(cookies), "valid": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ===========================================================================
# v2.0 自动广告模块：商品库从本地数据库读取
# ===========================================================================
@app.get("/api/products/{shop_id}")
def get_products(shop_id: str):
    """获取指定店铺的全部商品（从本地 SQLite，毫秒级响应）。"""
    products = db.get_products_grouped_by_parent(shop_id)
    count = db.count_products(shop_id)
    shop = db.get_shop(shop_id)
    return {
        "shop_id": shop_id,
        "shop_name": shop["shop_name"] if shop else "",
        "total_products": count,
        "total_parents": len(products),
        "products": products,
    }


@app.get("/api/products/{shop_id}/payload")
def get_products_payload(shop_id: str, base_budget: float = 100.0):
    """把本地库商品转换成与「Excel 解析」完全一致的结构。

    自动广告工作台（v10）直接消费 ``shops`` 这个结构：
    ``{店铺名: {median_price, total_parents, products:[{parent_sku, product_name,
    raw_name, image_url, child_count, avg_price, median_ratio,
    suggested_budget, children, prices}]}}``

    复用 ``build_shop_payload``，保证「中位数客单价 / 均价 / 中位数比率 /
    建议预算」等计算口径与 Excel 路径 100% 一致。
    """
    rows = db.get_products_by_shop(shop_id)
    if not rows:
        return {"shops": {}, "shop_id": shop_id, "shop_name": "", "total_parents": 0}

    shop = db.get_shop(shop_id)
    shop_name = (shop["shop_name"] if shop else "") or shop_id

    groups: Dict[str, Dict[str, Any]] = {}
    all_prices: List[float] = []

    for r in rows:
        pkey = str(r.get("parent_sku") or r.get("asin") or "").strip()
        if not pkey:
            continue
        price = float(r.get("price") or 0)
        if price > 0:
            all_prices.append(price)

        g = groups.setdefault(
            pkey,
            {
                "parent_sku": pkey,
                "clean_name": r.get("clean_zh_name") or "商品",
                "raw_name": r.get("raw_title") or "",
                "image_url": r.get("image_url") or "",
                "children": [],
                "prices": [],
            },
        )
        g["children"].append(
            {
                "msku": r.get("msku") or r.get("sku") or "",
                "asin": r.get("asin") or "",
                "price": price,
            }
        )
        if price > 0:
            g["prices"].append(price)
        # 补图 / 补更干净的品名
        if not g["image_url"] and r.get("image_url"):
            g["image_url"] = r["image_url"]
        raw = r.get("raw_title") or ""
        if raw and (not g["raw_name"] or len(raw) < len(g["raw_name"])):
            g["raw_name"] = raw

    parsed = {"shops": {shop_name: {"groups": groups, "all_prices": all_prices}}}
    shops = build_shop_payload(parsed, base_budget=base_budget)

    return {
        "shops": shops,
        "shop_id": shop_id,
        "shop_name": shop_name,
        "total_parents": len(groups),
    }


@app.post("/api/sync/products")
def trigger_product_sync(req: SyncTriggerRequest):
    """手动触发商品增量同步。

    从赛狐 API 拉取全店在线商品，已存在的跳过，仅入库新上架商品。
    """
    try:
        if req.shop_id:
            shop = db.get_shop(req.shop_id)
            shop_name = shop["shop_name"] if shop else ""
            if req.account_id:
                with sf.use_account(req.account_id):
                    result = sync_service.sync_products_for_shop(
                        req.shop_id, shop_name
                    )
            else:
                result = sync_service.sync_products_for_shop(req.shop_id, shop_name)
        else:
            result = sync_service.sync_products_all_shops(req.account_id or "")
        return {"ok": True, "results": result if isinstance(result, list) else [result]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"同步失败：{exc}")


# ===========================================================================
# v2.0 手动广告模块：搜索词数据
# ===========================================================================
@app.post("/api/search-terms/query")
def query_search_terms(req: SearchTermQueryRequest):
    """多维筛选搜索词数据。"""
    rows = db.query_search_terms(
        shop_id=req.shop_id or None,
        start_date=req.start_date or None,
        end_date=req.end_date or None,
        only_orders=req.only_orders,
        keyword=req.keyword,
        order_by=req.order_by,
        order_desc=req.order_desc,
        limit=req.limit,
        dimension=req.dimension,
        aggregate=req.aggregate,
    )
    total = db.count_search_terms(
        shop_id=req.shop_id or None,
        start_date=req.start_date or None,
        end_date=req.end_date or None,
        only_orders=req.only_orders,
        dimension=req.dimension,
        aggregate=req.aggregate,
    )
    return {
        "rows": rows, "total": total, "limit": req.limit,
        "dimension": req.dimension, "aggregate": req.aggregate,
    }


@app.post("/api/sync/search-terms")
def trigger_search_sync(req: SyncTriggerRequest):
    """手动触发搜索词数据拉取。

    - 指定 ``shop_id``：只同步该店铺
    - 指定 ``target_date``：只同步该日期
    - 否则按 ``days`` 回补最近 N 天（默认 1 = 仅昨天）
    """
    try:
        if req.shop_id:
            shop = db.get_shop(req.shop_id)
            shop_name = shop["shop_name"] if shop else ""
            if req.target_date:
                dates = [req.target_date]
            else:
                days = max(1, min(int(req.days or 1), 30))
                today = datetime.date.today()
                dates = [
                    (today - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
                    for i in range(1, days + 1)
                ]
            if req.account_id:
                with sf.use_account(req.account_id):
                    result = [
                        sync_service.sync_search_terms_for_shop(
                            req.shop_id, shop_name, d
                        )
                        for d in dates
                    ]
            else:
                result = [
                    sync_service.sync_search_terms_for_shop(
                        req.shop_id, shop_name, d
                    )
                    for d in dates
                ]
        else:
            result = sync_service.sync_search_terms_all_shops(
                req.target_date or None, req.days or 1, req.account_id or ""
            )
        return {"ok": True, "results": result if isinstance(result, list) else [result]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"同步失败：{exc}")


# ===========================================================================
# v2.4 多账号全量同步
# ===========================================================================
_ACCOUNT_SYNC: Dict[str, Any] = {
    "running": False,
    "kind": "",
    "started_at": "",
    "finished_at": "",
    "accounts": [],
    "results": [],
    "error": "",
}


def _run_account_sync(
    kind: str, target_date: str, days: int, account_ids: List[str]
) -> None:
    _ACCOUNT_SYNC.update({
        "running": True, "kind": kind, "error": "",
        "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "finished_at": "", "accounts": [], "results": [],
    })
    try:
        out = sync_service.sync_all_accounts(
            kind=kind,
            target_date=target_date or None,
            days=days,
            account_ids=account_ids or None,
        )
        _ACCOUNT_SYNC["accounts"] = out.get("accounts", [])
        _ACCOUNT_SYNC["results"] = out.get("results", [])
    except Exception as exc:  # noqa: BLE001 - 后台任务不能把异常抛给线程
        _ACCOUNT_SYNC["error"] = str(exc)
    finally:
        _ACCOUNT_SYNC["running"] = False
        _ACCOUNT_SYNC["finished_at"] = datetime.datetime.now().isoformat(
            timespec="seconds"
        )
        invalidate_shop_access_cache()


@app.post("/api/sync/all-accounts")
def sync_all_accounts(req: SyncAllAccountsRequest):
    """一键同步**所有赛狐账号**（后台执行，前端轮询进度）。

    每个账号用自己的 Cookie 探测店铺、拉取数据，互不覆盖。
    """
    if _ACCOUNT_SYNC["running"]:
        return {"ok": False, "message": "已有全量同步任务在跑，请等待完成"}
    if not sf.list_accounts():
        raise HTTPException(status_code=400, detail="还没有配置任何赛狐账号")
    kind = req.kind if req.kind in ("search", "product", "both") else "search"
    threading.Thread(
        target=_run_account_sync,
        args=(
            kind,
            req.target_date,
            max(1, min(int(req.days or 1), 30)),
            req.account_ids,
        ),
        daemon=True,
    ).start()
    return {"ok": True, "message": "已在后台开始同步所有账号", "kind": kind}


@app.get("/api/sync/all-accounts/status")
def sync_all_accounts_status():
    """查询全量同步进度与结果摘要。"""
    data = dict(_ACCOUNT_SYNC)
    results = data.get("results") or []
    data["summary"] = {
        "accounts": len(data.get("accounts") or []),
        "rows": len(results),
        "errors": sum(1 for r in results if r.get("error")),
    }
    return data


# ===========================================================================
# v2.0 手动广告模块：一键创建
# ===========================================================================
@app.post("/api/manual-ads/export-bulk")
def export_manual_bulk(req: ManualAdExportRequest):
    """导出手动关键词广告 Bulk Excel。

    生成包含 Campaign、Ad Group、Product Ad、Keyword 层级的标准亚马逊批量表。
    """
    buffer = generate_manual_keyword_bulk(req.campaigns, req.settings)
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=amazon_bulk_ads_manual.xlsx"},
    )


@app.post("/api/manual-ads/create-via-api")
def create_manual_via_api(req: ManualAdCreateRequest):
    """一键通过赛狐接口直接创建手动广告。

    将手动广告活动、广告组、广告商品、投放关键词 / ASIN 推送到赛狐后台。

    赛狐的 ``keywordTarget`` 是二选一开关，同一活动无法同时提交关键词与商品投放；
    当一个活动两者都有时，这里自动拆成「关键词活动」+「原名称_ASIN 活动」两次创建。
    Bulk 导出不受此限制（亚马逊允许同一广告组混合两种投放行）。
    """
    if not sf.has_valid_cookies():
        raise HTTPException(
            status_code=401,
            detail="赛狐 Cookie 未配置或已过期，请先在 Cookie 设置中导入登录态",
        )

    results = []
    for seq, campaign in enumerate(req.campaigns, 1):
        cname = campaign.get("campaign_name", "") or \
            f"{campaign.get('parent_sku', '')}_手动_{seq}"
        try:
            # 前端字段（keyword_text / match_type）→ 赛狐接口字段（keywordText / matchType）
            keywords = [
                {
                    "keywordText": kw.get("keywordText") or kw.get("keyword_text", ""),
                    "matchType": kw.get("matchType") or kw.get("match_type", "exact"),
                    "bid": str(kw.get("bid", 15)),
                }
                for kw in (campaign.get("keywords", []) or [])
            ]
            raw_products = campaign.get("products")
            if raw_products is None:
                raw_products = campaign.get("children", []) or []
            products = [
                {
                    "sku": p.get("sku") or p.get("msku", ""),
                    "asin": p.get("asin", ""),
                }
                for p in raw_products
            ]
            # 手动 ASIN 商品投放（与 Bulk 用的是同一批 ASIN）
            asins = [
                a for a in (str(x).strip().upper() for x in (campaign.get("asins") or []))
                if len(a) == 10 and a.isalnum() and a.startswith("B0")
            ]
            asin_bid = str(campaign.get("asin_bid") or campaign.get("default_bid", 25))
            targetings = [
                {"type": "asin", "asin": a, "bid": asin_bid}
                for a in dict.fromkeys(asins)
            ]

            common = dict(
                shop_id=req.shop_id,
                daily_budget=str(campaign.get("daily_budget", 100)),
                start_date=campaign.get("start_date", ""),
                end_date=campaign.get("end_date", ""),
                portfolio_id=campaign.get("portfolio_id", ""),
                strategy=campaign.get("strategy", "legacyForSales"),
                default_bid=str(campaign.get("default_bid", 25)),
                products=products,
                placement_top=campaign.get("placement_top"),
                placement_product_page=campaign.get("placement_product_page"),
                placement_rest_of_search=campaign.get("placement_rest_of_search"),
            )

            # 赛狐 keywordTarget 是二选一开关：同一活动不能同时提交关键词与商品投放。
            # 两者都有时自动拆成两个活动（ASIN 活动名称加 _ASIN 后缀），避免静默丢投放。
            submissions = []
            if keywords:
                submissions.append((cname, True, keywords, []))
            if targetings:
                submissions.append((cname + ("_ASIN" if keywords else ""),
                                    False, [], targetings))
            if not submissions:
                submissions.append((cname, True, [], []))

            for sub_name, kw_target, kws, tgs in submissions:
                payload = sf.build_manual_ad_payload(
                    campaign_name=sub_name,
                    group_name=campaign.get("group_name") or sub_name,
                    keyword_target=kw_target,
                    keywords=kws,
                    targetings=tgs,
                    **common,
                )
                resp = sf.create_sp_ad(payload)
                check = sf.check_create_result(resp)
                results.append({
                    "campaign_name": sub_name,
                    "target_type": "keyword" if kw_target else "asin",
                    "split": len(submissions) > 1,
                    **check,
                })
        except Exception as exc:
            results.append({
                "campaign_name": cname,
                "success": False,
                "errors": [str(exc)],
            })

    return {"ok": True, "results": results}


# ===========================================================================
# v2.0 定时任务管理
# ===========================================================================
@app.get("/api/scheduler/status")
def scheduler_status():
    """获取定时任务状态（含当前计划配置、下次执行时间、上次运行结果）。"""
    return scheduler.get_scheduler_status()


@app.post("/api/scheduler/start")
def scheduler_start():
    """启动定时任务调度器。"""
    scheduler.start_scheduler()
    return {"ok": True, **scheduler.get_scheduler_status()}


@app.post("/api/scheduler/stop")
def scheduler_stop():
    """停止定时任务调度器。"""
    scheduler.stop_scheduler()
    return {"ok": True, "running": False, **scheduler.get_scheduler_status()}


@app.post("/api/scheduler/config")
def scheduler_save_config(payload: Dict[str, Any]):
    """保存同步计划（星期 / 小时 / 分钟 / 是否启用），立即生效并落盘。

    请求体示例::

        {
          "product_sync": {"enabled": true, "day_of_week": "mon", "hour": 3, "minute": 0},
          "search_sync":  {"enabled": true, "hour": 2, "minute": 0}
        }
    """
    try:
        status = scheduler.apply_config(payload)
        return {"ok": True, **status}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"保存同步计划失败：{exc}") from exc


@app.post("/api/scheduler/run-now/{job_key}")
def scheduler_run_now(job_key: str):
    """立即执行一次同步任务（后台线程，不阻塞请求）。

    ``job_key`` 取值：``product_sync``（商品同步）/ ``search_sync``（搜索词同步）。
    """
    result = scheduler.run_job_now(job_key)
    if not result.get("ok"):
        # 未知任务 400；正在运行中 409
        code = 409 if result.get("busy") else 400
        raise HTTPException(status_code=code, detail=result.get("message", "执行失败"))
    return result


# ===========================================================================
# 本地纯净名词词库管理（v1 兼容，保持不变）
# ===========================================================================
@app.post("/api/dictionary/import")
async def import_dictionary(file: UploadFile = File(...)):
    """导入 AI 清洗后的纯净中日对照 JSON 并持久化。"""
    try:
        content = await file.read()
        try:
            data = json.loads(content.decode("utf-8-sig"))
        except UnicodeDecodeError:
            data = json.loads(content.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("导入的词典格式必须为 JSON 对象键值对")
        result = noun_matcher.update_dictionary(data)
        return {
            "status": "success",
            "imported_count": result["imported"],
            "purged_keys": result["purged"],
            "total_count": result["total"],
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"词库导入失败: {exc}") from exc


@app.get("/api/dictionary/stats")
def get_dict_stats():
    return {"total_count": len(noun_matcher.dictionary)}


@app.get("/api/dictionary")
def get_dictionary():
    return {**noun_matcher.stats(), "entries": noun_matcher.list_entries()}


@app.get("/api/dictionary/export")
def export_dictionary():
    payload = json.dumps(noun_matcher.export_dictionary(), ensure_ascii=False, indent=2)
    return JSONResponse(
        content=json.loads(payload),
        headers={"Content-Disposition": "attachment; filename=ja_zh_dict.json"},
    )


@app.post("/api/dictionary")
def upsert_dictionary(entry: DictEntry):
    ja, zh = entry.ja.strip(), entry.zh.strip()
    if not ja or not zh:
        raise HTTPException(status_code=400, detail="日文词与中文词都不能为空")
    noun_matcher.upsert(ja, zh)
    return {"ok": True, "total_count": len(noun_matcher.dictionary)}


@app.delete("/api/dictionary")
def delete_dictionary(ja: str):
    if not noun_matcher.delete(ja):
        raise HTTPException(status_code=404, detail=f"词库中不存在：{ja}")
    return {"ok": True, "total_count": len(noun_matcher.dictionary)}


@app.post("/api/dictionary/reload")
def reload_dictionary():
    return {"ok": True, "total_count": noun_matcher.reload()}


@app.post("/api/dictionary/rematch")
def rematch_names(payload: RematchRequest):
    matched: Dict[str, str] = {}
    hit_skus: List[str] = []
    miss_skus: List[str] = []
    for item in payload.items:
        key = (item.key or item.parent_sku or "").strip()
        raw = (item.raw_name or "").strip()
        if not key or not raw:
            continue
        zh = noun_matcher.match_noun_only(raw)
        if zh:
            matched[key] = zh
            hit_skus.append(key)
        else:
            matched[key] = noun_matcher.match_first_noun(raw)
            miss_skus.append(key)
    return {
        "matched_names": matched,
        "hit_skus": hit_skus,
        "miss_skus": miss_skus,
        "matched_count": len(matched),
        "hit_count": len(hit_skus),
        "miss_count": len(miss_skus),
        "dict_total": len(noun_matcher.dictionary),
    }


# ===========================================================================
# 启动
# ===========================================================================
def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        pass


@app.on_event("startup")
def _on_startup():
    """应用启动时初始化数据库和调度器。"""
    db.init_db()
    scheduler.start_scheduler()


@app.on_event("shutdown")
def _on_shutdown():
    """应用关闭时停止调度器。"""
    scheduler.stop_scheduler()


if __name__ == "__main__":
    import uvicorn

    host, port = "127.0.0.1", 8000
    url = f"http://{host}:{port}"
    threading.Timer(1.5, _open_browser, args=(url,)).start()
    print(f"\n  Amazon Bulk Ads Generator v2.0 已启动 -> {url}")
    print(f"  本地词库：{len(noun_matcher.dictionary)} 条  ({noun_matcher.dict_path})")
    print(f"  Cookie 状态：{'有效' if sf.has_valid_cookies() else '未配置'}")
    print(f"  定时任务：商品同步=每周一03:00, 搜索词同步=每日02:00 (Asia/Tokyo)\n")
    uvicorn.run("main:app", host=host, port=port, reload=False)
