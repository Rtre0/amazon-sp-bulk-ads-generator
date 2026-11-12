"""Amazon Bulk Operations Ads Generator —— FastAPI 主入口（v2.7.0）。

一键运行：``python main.py``
启动后自动在浏览器打开 http://127.0.0.1:8000

版本线说明
--------------------------------------------------------------------
GitHub 上的最后一个旧版发布为 **v10.0.0**（commit c60aff0，2026-10-30），
属于「Excel → Bulk 单文件生成器」版本线。之后项目按功能模块重新编号为
v2.x 系列；当前 **v2.7.0** 已包含多账号同步、本地商品库、自动/手动广告
双模块、接口直接创建与父 SKU 广告开启状态判定。详见 CHANGELOG.md。

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

import contextlib
import datetime
import hashlib
import json
import os
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.budget_engine import build_shop_payload
from core.bulk_generator import generate_bulk_excel, generate_manual_keyword_bulk
from core.parser import parse_sellfox_excel_multi_shop
from core.translator import noun_matcher
from core import ad_coverage
from core import database as db
from core import paths
from core import sellfox_client as sf
from core import sync_service
from core import scheduler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# 版本号唯一来源：FastAPI 元数据与 /api/health 都读它，避免两处硬编码漂移。
# 发布新版本时请同步更新 CHANGELOG.md 与 docs/RELEASE_NOTES_*.md。
__version__ = "2.7.0"

app = FastAPI(
    title="Amazon SP Bulk Ads Generator",
    description="赛狐 ERP 数据同步 + 自动/手动广告双模块工作台",
    version=__version__,
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


class AutoAdCreateRequest(BaseModel):
    """自动广告接口直推赛狐请求。

    ``products`` 与 Bulk 导出用的是同一份结构（parent_sku / product_name /
    assigned_budget / children），只是改成调用赛狐创建接口而不是生成表格。
    """
    shop_id: str
    products: List[Dict[str, Any]]
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
    label: str = ""      # 账号备注名，如「账号A / 备用账号」


class AccountCreateRequest(BaseModel):
    """新增赛狐账号（粘贴该账号的 Cookie）。"""
    cookie_str: str
    label: str = ""
    merge: bool = True


class AccountRenameRequest(BaseModel):
    """重命名账号备注。"""
    label: str


class AccountLoginRequest(BaseModel):
    """用赛狐账号密码登录（图形验证码人工输入）。"""
    username: str = ""
    password: str = ""
    label: str = ""
    account_id: str = ""      # 指定更新已有账号；空 = 按登录态指纹自动判断
    remember: bool = True     # 记住账号密码（存 SELLFOX_HOME/config，不进 Git）
    timeout: int = 300        # 等待人工完成验证码的秒数


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
    # v2.6：按「用户所选起止日期」同步（手动广告页的查询并同步用它）
    start_date: str = ""
    end_date: str = ""
    # False = 跳过本地已有数据的日期（默认，快）；True = 强制重新拉取覆盖
    force: bool = False


class SyncAllAccountsRequest(BaseModel):
    """全量同步所有赛狐账号。"""
    kind: str = "search"        # search=搜索词双维度 / product=商品 / both=两者
    target_date: str = ""
    days: int = 1
    account_ids: List[str] = []  # 空 = 全部账号


class SyncRangeRequest(BaseModel):
    """按显式日期区间补同步搜索词（含首尾）。"""
    start_date: str             # YYYY-MM-DD
    end_date: str               # YYYY-MM-DD
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
        "version": __version__,
        "modules": ["auto_ads", "manual_ads", "cookie_mgmt", "scheduler"],
        "cookie_valid": sf.has_valid_cookies(),
        "dict_count": len(noun_matcher.dictionary),
        **db.db_stats(),
    }


@app.get("/api/system/paths")
def system_paths():
    """当前数据/配置落在哪（用于确认「项目目录是否已与数据隔离」）。

    只返回路径与体积，不含任何凭据内容。
    """
    from core import paths

    info = paths.describe()
    info["project_dir"] = paths.BASE_DIR
    info["isolated"] = info["home"] != paths.BASE_DIR
    return info


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


# 亚马逊 Bulk 的「竞价方案」文案 → 赛狐接口的 strategy 枚举
_AUTO_STRATEGY_MAP = {
    "Fixed bid": "manual",                       # 固定竞价
    "Dynamic bids - down only": "legacyForSales",  # 动态竞价-只降低
    "Dynamic bids - up and down": "autoForSales",  # 动态竞价-提高和降低
}


@app.post("/api/auto-ads/create-via-api")
def create_auto_via_api(req: AutoAdCreateRequest):
    """自动广告一键通过赛狐接口创建（与手动广告的「接口直推」同一套链路）。

    一个父体 = 一个自动活动：
    - Campaign：targetingType=auto、每日预算 = 该父体分配预算、竞价方案跟随设置
    - Ad Group：默认竞价 + 该父体下**全部子体**（SKU/ASIN）挂为广告商品
    - 自动 4 组：紧密 / 宽泛 / 同类 / 关联，各自独立竞价

    与 Bulk 导出的口径保持一致，活动名同为 ``父SKU_品名_自动_YYYYMMDD``。
    """
    if not sf.has_valid_cookies():
        raise HTTPException(
            status_code=401,
            detail="赛狐 Cookie 未配置或已过期，请先在「同步与 Cookie 设置」导入登录态",
        )
    if not req.shop_id:
        raise HTTPException(status_code=400, detail="请先选择店铺")

    settings = req.settings or {}
    strategy = _AUTO_STRATEGY_MAP.get(
        str(settings.get("bidding_strategy") or "Fixed bid"), "manual"
    )
    default_bid = settings.get("default_bid", 25)
    portfolio_id = settings.get("portfolio_id") or ""
    include_targeting = bool(settings.get("include_targeting", True))
    include_placement = bool(settings.get("include_placement", True))

    def _pct(key: str) -> Optional[str]:
        """广告位溢价：0 视为不做加成（与 Bulk 一致，不提交该字段）。"""
        if not include_placement:
            return None
        try:
            val = int(float(settings.get(key) or 0))
        except (TypeError, ValueError):
            return None
        return str(val) if val > 0 else None

    # include_targeting=False 时，4 组竞价留空 → 继承广告组默认竞价
    def _bid(key: str, fallback: Any) -> str:
        if not include_targeting:
            return ""
        return str(settings.get(key, fallback))

    today_str = datetime.datetime.now().strftime("%Y%m%d")
    results: List[Dict[str, Any]] = []

    for seq, item in enumerate(req.products, 1):
        parent_sku = str(item.get("parent_sku") or "").strip()
        prod_name = str(item.get("product_name") or "商品").strip() or "商品"
        campaign_name = f"{parent_sku}_{prod_name}_自动_{today_str}"
        try:
            children = item.get("children") or []
            products = [
                {
                    "sku": c.get("msku") or c.get("sku") or "",
                    "asin": c.get("asin") or "",
                }
                for c in children
                if (c.get("msku") or c.get("sku") or c.get("asin"))
            ]
            if not products:
                results.append({
                    "campaign_name": campaign_name,
                    "parent_sku": parent_sku,
                    "success": False,
                    "errors": ["该父体没有可投放的子体（缺 MSKU / ASIN）"],
                })
                continue

            budget = item.get(
                "assigned_budget",
                item.get("custom_budget", item.get("suggested_budget", 100)),
            )
            payload = sf.build_auto_ad_payload(
                shop_id=req.shop_id,
                campaign_name=campaign_name,
                daily_budget=str(budget or 100),
                portfolio_id=portfolio_id,
                strategy=strategy,
                default_bid=str(default_bid),
                bid_close=_bid("bid_close", 25),
                bid_loose=_bid("bid_loose", 20),
                bid_sub=_bid("bid_substitutes", 25),
                bid_comp=_bid("bid_complements", 20),
                products=products,
                placement_top=_pct("placement_top_pct"),
                placement_product_page=_pct("placement_product_pct"),
            )
            resp = sf.create_sp_ad(payload)
            check = sf.check_create_result(resp)
            results.append({
                "campaign_name": campaign_name,
                "parent_sku": parent_sku,
                "product_count": len(products),
                "budget": budget,
                **check,
            })
        except Exception as exc:  # noqa: BLE001 - 单个失败不影响其余商品
            results.append({
                "campaign_name": campaign_name,
                "parent_sku": parent_sku,
                "success": False,
                "errors": [str(exc)],
            })

    ok = sum(1 for r in results if r.get("success"))
    return {
        "ok": True,
        "shop_id": req.shop_id,
        "summary": {"total": len(results), "success": ok, "failed": len(results) - ok},
        "results": results,
    }


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


# ---------------------------------------------------------------------------
# v2.6 半自动登录：账号密码 + 人工输入图形验证码
#   赛狐登录页强制图形验证码，纯接口登录走不通。这里用真实浏览器完成登录，
#   成功后自动把 Cookie 写进账号池，省掉手工复制 Cookie。
# ---------------------------------------------------------------------------
_LOGIN: Dict[str, Any] = {
    "running": False,
    "stage": "",
    "message": "",
    "username": "",
    "label": "",
    "account": None,
    "shops": 0,
    "error": "",
    "probe_error": "",
    "started_at": "",
    "finished_at": "",
    "cancel": False,
}


def _run_account_login(req: "AccountLoginRequest") -> None:
    def on_stage(stage: str, message: str) -> None:
        _LOGIN["stage"] = stage
        _LOGIN["message"] = message

    try:
        res = sf.login_and_capture(
            req.username,
            req.password,
            label=req.label,
            account_id=req.account_id,
            timeout=req.timeout,
            on_stage=on_stage,
            should_cancel=lambda: bool(_LOGIN.get("cancel")),
        )
        if not res.get("ok"):
            _LOGIN["error"] = res.get("error") or "登录失败"
            return

        acc = res.get("account") or {}
        _LOGIN["account"] = acc

        if req.remember:
            pwd = req.password or sf.get_saved_password(req.username)
            sf.remember_credentials(req.username, pwd, req.label or req.username)

        # 登录成功后顺手探测店铺，省一次点击
        if acc.get("id"):
            _LOGIN["stage"] = "probing"
            _LOGIN["message"] = "正在探测该账号可访问的店铺…"
            try:
                with sf.use_account(acc["id"]):
                    found = sync_service.discover_and_register_shops(acc["id"])
                _LOGIN["shops"] = len(found)
                _LOGIN["account"] = next(
                    (a for a in sf.list_accounts() if a["id"] == acc["id"]), acc
                )
            except Exception as exc:  # noqa: BLE001 - 探测失败不影响登录结果
                _LOGIN["probe_error"] = str(exc)

        invalidate_shop_access_cache()
    except Exception as exc:  # noqa: BLE001 - 后台线程不能抛异常
        _LOGIN["error"] = str(exc)
    finally:
        _LOGIN["running"] = False
        _LOGIN["finished_at"] = datetime.datetime.now().isoformat(timespec="seconds")


@app.post("/api/accounts/login")
def accounts_login(req: AccountLoginRequest):
    """启动半自动登录：会弹出一个 Chrome 窗口，请在里面输入图形验证码。"""
    if _LOGIN["running"]:
        return {"ok": False, "message": "已有登录任务在进行，请先完成或取消"}
    _LOGIN.update({
        "running": True, "stage": "opening", "message": "正在启动浏览器…",
        "username": req.username, "label": req.label,
        "account": None, "shops": 0, "error": "", "probe_error": "",
        "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "finished_at": "", "cancel": False,
    })
    threading.Thread(target=_run_account_login, args=(req,), daemon=True).start()
    return {
        "ok": True,
        "message": "浏览器正在打开，请在窗口里完成验证码后点「登录」",
    }


@app.get("/api/accounts/login/status")
def accounts_login_status():
    """查询半自动登录进度。"""
    data = {k: v for k, v in _LOGIN.items() if k != "cancel"}
    return data


@app.post("/api/accounts/login/cancel")
def accounts_login_cancel():
    """取消正在进行的登录。"""
    if not _LOGIN["running"]:
        return {"ok": False, "message": "当前没有进行中的登录"}
    _LOGIN["cancel"] = True
    return {"ok": True, "message": "已请求取消"}


@app.get("/api/accounts/credentials")
def list_account_credentials():
    """已记住的账号列表（不返回密码）。"""
    return {"ok": True, "credentials": sf.list_saved_credentials()}


@app.delete("/api/accounts/credentials/{username}")
def delete_account_credential(username: str):
    """忘掉某个账号的密码。"""
    sf.remember_credentials(username, "", "")
    return {"ok": True, "credentials": sf.list_saved_credentials()}


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
# 父 SKU 广告开启状态（v2.7）
# ===========================================================================
# 自动广告工作台要回答一个问题：「这个父体是不是已经开过广告了？」
# 数据来自赛狐：活动列表（state）+ 活动下广告商品（sku）。判定逻辑在
# core/ad_coverage.py，这里只负责取数 + 缓存。
_AD_COVERAGE_CACHE: Dict[str, Dict[str, Any]] = {}
_AD_COVERAGE_TTL = 600          # 10 分钟
_AD_COVERAGE_LOCK = threading.Lock()


@app.get("/api/shops/{shop_id}/ad-coverage")
def get_ad_coverage(shop_id: str, refresh: int = 0, days: int = 60):
    """返回该店铺每个父 SKU 的广告开启状态（已开启 / 已暂停 / 未开启）。

    - ``refresh=1`` 强制重新拉取赛狐数据
    - 结果缓存 10 分钟，避免每次切店铺都打接口
    - 拉取失败时返回 ``ok=false``，前端显示「未知」而不是误判成「未开启」
    """
    now = time.time()
    with _AD_COVERAGE_LOCK:
        cached = _AD_COVERAGE_CACHE.get(shop_id)
    if cached and not refresh and now - cached["ts"] < _AD_COVERAGE_TTL:
        return {**cached["data"], "cached": True}

    parents = db.get_parent_sku_map(shop_id)
    if not parents:
        return {
            "ok": False,
            "error": "本地库暂无该店铺商品，请先同步商品后再判断广告状态",
            "shop_id": shop_id,
            "coverage": {},
        }
    if not sf.has_valid_cookies():
        return {
            "ok": False,
            "error": "赛狐 Cookie 未配置或已过期，无法读取广告状态",
            "shop_id": shop_id,
            "coverage": {},
        }

    try:
        shop = db.get_shop(shop_id) or {}
        acc_id = shop.get("account_id") or ""
        ctx = sf.use_account(acc_id) if acc_id else contextlib.nullcontext()
        with ctx:
            data = ad_coverage.fetch_coverage(
                shop_id, parents, days=max(7, min(int(days or 60), 180))
            )
    except Exception as exc:  # noqa: BLE001 - 失败要降级成「未知」
        return {
            "ok": False,
            "error": f"读取赛狐广告数据失败：{exc}",
            "shop_id": shop_id,
            "coverage": {},
        }

    payload = {"ok": True, "generated_at": datetime.datetime.now().isoformat(timespec="seconds"), **data}
    with _AD_COVERAGE_LOCK:
        _AD_COVERAGE_CACHE[shop_id] = {"ts": now, "data": payload}
    return {**payload, "cached": False}


# ===========================================================================
# 商品图片代理（v2.6）
# ===========================================================================
# 为什么需要它：商品图存在亚马逊图床（m.media-amazon.com）。国内网络直连经常
# 超时、被运营商拦截，或者被图床的防盗链挡住，浏览器里就只剩白色方块。改成
# 「浏览器 → 本机后端 → 亚马逊」由服务端取图并落盘缓存，只要同步能跑通，图片
# 就一定显示得出来；同一张图只下载一次，后续走本地文件。
_IMG_ALLOWED_HOSTS = (
    "m.media-amazon.com",
    "images-na.ssl-images-amazon.com",
    "images-eu.ssl-images-amazon.com",
    "images-fe.ssl-images-amazon.com",
    "ws-na.amazon-adsystem.com",
)
_IMG_CACHE_DIR = os.path.join(paths.DATA_DIR, "image_cache")
_IMG_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@app.get("/api/image-proxy")
def image_proxy(u: str):
    """代理亚马逊商品图并本地缓存，避免浏览器直连失败导致图片空白。

    只允许亚马逊图床域名（防 SSRF）；按 URL 的 sha1 落盘，命中缓存直接返回。
    """
    raw = (u or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="缺少图片地址")
    if raw.startswith("//"):
        raw = "https:" + raw
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in _IMG_ALLOWED_HOSTS:
        raise HTTPException(status_code=403, detail="不允许的图片域名")

    # 缩略图换成 200px 版本，40px 的小图放大后太糊
    cache_key = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    cache_path = os.path.join(_IMG_CACHE_DIR, cache_key)
    if os.path.exists(cache_path):
        with open(cache_path, "rb") as fh:
            data = fh.read()
        return Response(
            content=data,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=604800"},
        )

    try:
        req = urllib.request.Request(
            raw,
            headers={"User-Agent": _IMG_UA, "Accept": "image/*,*/*;q=0.8"},
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = resp.read()
            ctype = resp.headers.get("Content-Type") or "image/jpeg"
    except Exception as exc:  # noqa: BLE001 - 取图失败不该 500 打断页面
        raise HTTPException(status_code=502, detail=f"取图失败：{exc}") from exc

    if not data:
        raise HTTPException(status_code=502, detail="取图失败：空响应")
    try:
        os.makedirs(_IMG_CACHE_DIR, exist_ok=True)
        with open(cache_path, "wb") as fh:
            fh.write(data)
    except OSError:
        pass  # 缓存写失败不影响返回
    return Response(
        content=data,
        media_type=ctype.split(";")[0].strip() or "image/jpeg",
        headers={"Cache-Control": "public, max-age=604800"},
    )


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


def _parse_date_range(start_date: str, end_date: str, max_days: int = 92) -> List[str]:
    """把起止日期展开成逐日列表（含两端），最多 ``max_days`` 天。"""
    try:
        start = datetime.date.fromisoformat(str(start_date))
        end = datetime.date.fromisoformat(str(end_date))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"日期格式错误：{exc}") from exc
    if end < start:
        start, end = end, start
    days = (end - start).days + 1
    if days > max_days:
        raise HTTPException(
            status_code=400, detail=f"区间过长（{days} 天），单次最多 {max_days} 天"
        )
    return [(start + datetime.timedelta(days=i)).isoformat() for i in range(days)]


@app.post("/api/sync/search-terms")
def trigger_search_sync(req: SyncTriggerRequest):
    """手动触发搜索词数据拉取（关键词 + 投放 ASIN 双维度）。

    三种用法：
    - ``start_date`` + ``end_date``：按用户所选区间逐日同步（手动广告页用）
    - ``target_date``：只同步该日期
    - 都不传：按 ``days`` 回补最近 N 天（默认 1 = 仅昨天）

    ``force=False``（默认）时**跳过本地已有数据的日期**，只补缺口，
    这样反复点「查询并同步」不会每次都把整个区间重拉一遍。
    """
    try:
        if req.start_date and req.end_date:
            dates = _parse_date_range(req.start_date, req.end_date)
        elif req.target_date:
            dates = [req.target_date]
        else:
            days = max(1, min(int(req.days or 1), 30))
            today = datetime.date.today()
            dates = [
                (today - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
                for i in range(1, days + 1)
            ]

        # 目标店铺：指定了就只同步它；没指定就同步当前账号下的全部店铺，
        # 每家用**自己归属的账号** Cookie，否则会大面积「没有店铺访问权限」。
        if req.shop_id:
            shop = db.get_shop(req.shop_id)
            targets = [(req.shop_id, (shop or {}).get("shop_name", "") or "")]
        else:
            aid = req.account_id or sf.active_account_id()
            targets = [
                (s["shop_id"], s.get("shop_name") or "")
                for s in db.list_shops(aid)
            ]

        results: List[Dict[str, Any]] = []
        skipped = 0
        synced = 0
        for sid, sname in targets:
            todo = dates
            if not req.force:
                todo = [d for d in dates if db.count_rows_for_date(sid, d)["total"] == 0]
                skipped += len(dates) - len(todo)
            if not todo:
                continue
            shop_row = db.get_shop(sid) or {}
            acc_id = shop_row.get("account_id") or req.account_id or ""
            ctx = sf.use_account(acc_id) if acc_id else contextlib.nullcontext()
            with ctx:
                for d in todo:
                    # 单店单日失败（权限 / 限流 / 网络）不该让整个区间前功尽弃，
                    # 记一条 error 继续跑下一天，前端会把它显示成「权限提示」
                    try:
                        results.append(
                            sync_service.sync_search_terms_for_shop(sid, sname, d)
                        )
                        synced += 1
                    except Exception as exc:  # noqa: BLE001
                        results.append({
                            "shop_id": sid,
                            "shop_name": sname,
                            "report_date": d,
                            "error": str(exc),
                        })

        return {
            "ok": True,
            "dates": dates,
            "shops": len(targets),
            "synced": synced,      # 实际拉取的「店铺 × 日期」组合数
            "skipped": skipped,    # 因本地已有数据而跳过的组合数
            "results": results,
        }
    except HTTPException:
        raise
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


# ---------------------------------------------------------------------------
# v2.5 按日期区间补同步（专治历史缺口：某店某几天从没跑过）
# ---------------------------------------------------------------------------
_RANGE_SYNC: Dict[str, Any] = {
    "running": False,
    "start_date": "",
    "end_date": "",
    "started_at": "",
    "finished_at": "",
    "accounts": [],
    "results": [],
    "error": "",
}


def _run_range_sync(
    start_date: str, end_date: str, account_ids: List[str]
) -> None:
    _RANGE_SYNC.update({
        "running": True, "error": "",
        "start_date": start_date, "end_date": end_date,
        "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "finished_at": "", "accounts": [], "results": [],
    })
    try:
        out = sync_service.sync_all_accounts_range(
            start_date, end_date, account_ids or None
        )
        _RANGE_SYNC["accounts"] = out.get("accounts", [])
        _RANGE_SYNC["results"] = out.get("results", [])
    except Exception as exc:  # noqa: BLE001 - 后台任务不能把异常抛给线程
        _RANGE_SYNC["error"] = str(exc)
    finally:
        _RANGE_SYNC["running"] = False
        _RANGE_SYNC["finished_at"] = datetime.datetime.now().isoformat(
            timespec="seconds"
        )
        invalidate_shop_access_cache()


@app.post("/api/sync/search-terms/range")
def sync_search_terms_range(req: SyncRangeRequest):
    """按指定日期区间补同步搜索词 + 投放 ASIN（后台执行，前端轮询进度）。"""
    if _RANGE_SYNC["running"]:
        return {"ok": False, "message": "已有区间补同步任务在跑，请等待完成"}
    if _ACCOUNT_SYNC["running"]:
        return {"ok": False, "message": "全量同步正在运行，请等它结束后再补区间"}
    if not sf.list_accounts():
        raise HTTPException(status_code=400, detail="还没有配置任何赛狐账号")

    try:
        d0 = datetime.date.fromisoformat(str(req.start_date).strip())
        d1 = datetime.date.fromisoformat(str(req.end_date).strip())
    except ValueError:
        raise HTTPException(
            status_code=400, detail="日期格式应为 YYYY-MM-DD，例如 2026-11-01"
        )
    if d0 > d1:
        d0, d1 = d1, d0
    span = (d1 - d0).days + 1
    if span > 90:
        raise HTTPException(status_code=400, detail=f"单次最多补 90 天，当前 {span} 天")

    start_s = d0.strftime("%Y-%m-%d")
    end_s = d1.strftime("%Y-%m-%d")
    threading.Thread(
        target=_run_range_sync,
        args=(start_s, end_s, req.account_ids),
        daemon=True,
    ).start()
    return {
        "ok": True,
        "message": f"已开始补同步 {start_s} ~ {end_s}（{span} 天）",
        "start_date": start_s,
        "end_date": end_s,
        "days": span,
    }


@app.get("/api/sync/search-terms/range/status")
def sync_search_terms_range_status():
    """查询区间补同步进度与结果摘要。"""
    data = dict(_RANGE_SYNC)
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
