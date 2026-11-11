"""赛狐 ERP API 客户端。

严格遵循 ``ads_data_fetch_guide.md`` 第 0 节规范实现：
- Cookie 管理：读取本地 ``config/sf_cookies.json`` + Playwright CDP 自动抓取 + 手动粘贴
- 双协议请求：``post_json`` 用于 CPC 网关广告接口，``post_form`` 用于商品接口
- 自动反序列化嵌套 ``data`` 字符串
- 二次过滤校验（接口过滤参数不严格生效）

接口清单
---------
**取数（ads_data_fetch_guide.md）**
- ``fetch_product_page`` — 在线商品分页拉取（form 编码，BASE 参数）
- ``fetch_all_campaigns`` — 活动列表（含全量指标）
- ``fetch_ad_products`` — 广告产品列表（campaignId → SKU/ASIN 映射）
- ``fetch_search_terms`` — 搜索词数据（searchQueryType=searchKeyword）
- ``fetch_suggested_bid`` — 投放建议竞价

**创建（sellfox_sp_create_api.md）**
- ``create_sp_auto_ad`` — 创建自动投放 SP 广告
- ``create_sp_manual_ad`` — 创建手动关键词 SP 广告
"""

from __future__ import annotations

import datetime
import json
import os
import re
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COOKIE_FILE = os.path.join(BASE_DIR, "config", "sf_cookies.json")

# 网关前缀
GW_CPC = "https://www.sellfox.com/api/gw/sellfox/sellfox-cpc/api/sellfox"
GW_PRODUCT = "https://www.sellfox.com/api/gw/sellfox/sellfox-product/sellfox/product"
ORIGIN = "https://www.sellfox.com"

# 日本站 marketplaceId
JP_MARKETPLACE_ID = "A1VC38T7YXB528"

# 默认 UA
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
)

# 商品列表接口的 BASE 参数（完整，缺一不可，见指南 3.1 节）
PRODUCT_BASE_PARAMS: Dict[str, str] = {
    "currency": "",
    "pageType": "child",
    "asinType": "asin",
    "shopIds": "",
    "marketplaceId": "",
    "labelIds": "",
    "labelQuery": "0",
    "onlineStatus": "active,inActive",
    "switchFulfillmentTo": "",
    "isVariation": "",
    "match": "",
    "lowCostStore": "",
    "searchField": "asin",
    "searchValue": "",
    "searchMode": "exact",
    "pageSize": "200",
    "pageNo": "1",
    "fullCid": "",
    "organiser": "",
    "startTime": "",
    "endTime": "",
    "buyBoxWinners": "",
    "defaultFilterType": "1",
    "productBundle": "",
    "useAdvanced": "1",
    "devIds": "",
    "advancedSearchItem": "",
    "defaultPageType": "1",
    "complianceStatus": "",
}


# ---------------------------------------------------------------------------
# Cookie 管理
# ---------------------------------------------------------------------------
def load_cookies() -> Dict[str, str]:
    """从本地文件加载赛狐 Cookie。"""
    if not os.path.exists(COOKIE_FILE):
        return {}
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        return {}
    except (json.JSONDecodeError, IOError):
        return {}


def save_cookies(cookies: Dict[str, str]) -> None:
    """保存 Cookie 到本地文件。"""
    os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)


# Cookie 属性名（不是真正的 cookie，解析时要丢弃）
_COOKIE_ATTRS = {
    "path", "expires", "max-age", "domain", "secure", "httponly",
    "samesite", "priority", "partitioned", "version", "comment",
}
_KEY_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
# "Cookie: xxx" / "cookie: xxx"（Request Headers 里的行）
_HEADER_RE = re.compile(r"^\s*cookie\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
# cURL: -H 'Cookie: xxx' / --cookie 'xxx' / -b "xxx"
_CURL_H_RE = re.compile(
    r"-H\s+['\"](?:Cookie|cookie)\s*:\s*([^'\"]+)['\"]", re.MULTILINE
)
_CURL_B_RE = re.compile(
    r"(?:-b|--cookie)\s+['\"]([^'\"]+)['\"]", re.MULTILINE
)


def parse_cookie_input(text: str) -> Dict[str, str]:
    """把用户从浏览器粘贴的各种 Cookie 形态解析成 ``{name: value}``。

    支持：
    - 纯 Cookie 串 ``sf_u=xxx; sf_help=yyy``（DevTools 里「复制值」）
    - 带前缀 ``Cookie: sf_u=xxx; ...``
    - 整段 Request Headers（多行，自动挑出 cookie 行）
    - Chrome「Copy as cURL」整段（``-H 'Cookie: ...'`` / ``-b '...'``）
    - ``document.cookie`` 控制台输出
    - JSON 对象 ``{"sf_u": "xxx"}``
    - JSON 数组（EditThisCookie / Playwright 导出）``[{"name":"sf_u","value":"xxx"}]``

    只做解析，不落盘。
    """
    if not text or not text.strip():
        return {}
    raw = text.strip()

    # --- JSON ---
    if raw[0] in "{[":
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            data = None
        if isinstance(data, dict):
            # 可能是 Playwright 的 {"cookies": [...]} 包装
            if "cookies" in data and isinstance(data["cookies"], list):
                data = data["cookies"]
            else:
                return {
                    str(k).strip(): str(v).strip()
                    for k, v in data.items()
                    if _KEY_RE.match(str(k).strip()) and str(v).strip()
                }
        if isinstance(data, list):
            out: Dict[str, str] = {}
            for item in data:
                if not isinstance(item, dict):
                    continue
                k = str(item.get("name") or item.get("Name") or "").strip()
                v = str(item.get("value") or item.get("Value") or "").strip()
                if k and _KEY_RE.match(k):
                    out[k] = v
            return out

    # --- 从文本里抽出 cookie 值片段 ---
    chunks: List[str] = []
    chunks += [m.strip() for m in _CURL_H_RE.findall(raw)]
    chunks += [m.strip() for m in _CURL_B_RE.findall(raw)]
    chunks += [m.strip() for m in _HEADER_RE.findall(raw)]
    if not chunks:
        # 没有 "cookie:" 行 → 整段就是 cookie 串；顺手去掉可能有的一行前缀
        cleaned = re.sub(r"^\s*cookie\s*:\s*", "", raw, flags=re.IGNORECASE)
        chunks = [cleaned]

    cookies: Dict[str, str] = {}
    for chunk in chunks:
        chunk = chunk.strip().strip("'\"")
        for part in re.split(r"[;\n]", chunk):
            part = part.strip().strip("'\"")
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip()
            if not k or not _KEY_RE.match(k):
                continue
            if k.lower() in _COOKIE_ATTRS:
                continue
            # cURL 里 -b 可能带 URL / 路径，明显不是 cookie 的跳过
            if k.lower() in {"curl", "http", "https", "get", "post"}:
                continue
            cookies[k] = v
    return cookies


def update_cookies_from_dict(cookie_str: str, merge: bool = True) -> Dict[str, str]:
    """解析并保存前端粘贴的 Cookie，返回保存后的完整字典。

    ``merge=True``（默认）时与已有 Cookie 合并，只覆盖同名的键——
    避免用户只粘贴了部分 Cookie 就把整份登录态冲掉。
    """
    parsed = parse_cookie_input(cookie_str)
    if not parsed:
        return {}
    cookies = load_cookies() if merge else {}
    cookies.update(parsed)
    save_cookies(cookies)
    return cookies


def grab_cookies_via_cdp(cdp_url: str = "http://127.0.0.1:9222") -> Dict[str, str]:
    """通过 Playwright CDP 连接本地 Chrome，自动抓取 sellfox.com 的 Cookie。

    前提：Chrome 带 ``--remote-debugging-port=9222`` 启动，且已登录赛狐。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "Playwright 未安装。请运行: pip install playwright && playwright install chromium"
        )

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(cdp_url)
        except Exception as exc:
            raise RuntimeError(
                f"无法连接 Chrome CDP ({cdp_url})。"
                f"请确保 Chrome 带 --remote-debugging-port=9222 启动。错误: {exc}"
            ) from exc

        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        cookies_list = ctx.cookies()
        sf_cookies = {
            c["name"]: c["value"]
            for c in cookies_list
            if "sellfox" in (c.get("domain") or "")
        }

    if not sf_cookies:
        raise RuntimeError(
            "Chrome 中未找到 sellfox.com 的 Cookie。请确保浏览器已登录赛狐后台。"
        )

    save_cookies(sf_cookies)
    return sf_cookies


def get_cookie_header() -> str:
    """返回 Cookie 请求头字符串。"""
    cookies = load_cookies()
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def has_valid_cookies() -> bool:
    """检查是否有可用的 Cookie（至少包含 ``sf_u``）。"""
    cookies = load_cookies()
    return bool(cookies) and ("sf_u" in cookies or any("sf" in k for k in cookies))


# ---------------------------------------------------------------------------
# 请求封装
# ---------------------------------------------------------------------------
def _build_headers(content_type: str = "application/json") -> Dict[str, str]:
    """构建请求头（含 Cookie）。"""
    return {
        "Content-Type": content_type,
        "Accept": "application/json, text/plain, */*",
        "Cookie": get_cookie_header(),
        "Origin": ORIGIN,
        "Referer": f"{ORIGIN}/cpc-vue3/web/cpc-vue3/ad-create/index.html",
        "X-Language": "zh-CN",
        "User-Agent": DEFAULT_UA,
    }


def post_json(
    path: str,
    body: Dict[str, Any],
    base: str = GW_CPC,
    timeout: int = 60,
) -> Dict[str, Any]:
    """POST JSON 请求（用于 CPC 网关下的所有广告接口）。

    自动处理 ``isinstance(d.get("data"), str)`` 的二次 JSON 反序列化。
    """
    url = path if path.startswith("http") else base + "/" + path.lstrip("/")
    data = json.dumps(body).encode("utf-8")
    headers = _build_headers("application/json")

    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body_text = ""
        try:
            body_text = exc.read().decode("utf-8")
        except Exception:
            pass
        raise RuntimeError(
            f"赛狐接口 HTTP {exc.code}: {exc.reason}\nURL: {url}\n响应: {body_text[:500]}"
        ) from exc

    result = json.loads(raw)

    # 二次反序列化 data 字符串（指南 0.2 节）
    if isinstance(result.get("data"), str):
        try:
            result["data"] = json.loads(result["data"])
        except (json.JSONDecodeError, TypeError):
            pass

    return result


def post_form(
    url: str,
    body: Dict[str, Any],
    timeout: int = 60,
) -> Dict[str, Any]:
    """POST form-urlencoded 请求（专门用于在线商品接口）。

    必须使用 ``application/x-www-form-urlencoded`` 编码并携带完整 BASE 字典。
    """
    if not url.startswith("http"):
        url = GW_PRODUCT + "/" + url.lstrip("/")

    data = urllib.parse.urlencode(body).encode("utf-8")
    headers = _build_headers("application/x-www-form-urlencoded")
    # 商品接口的 Referer 不同
    headers["Referer"] = f"{ORIGIN}/amzup-web-main/web/product/index.html"

    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"赛狐商品接口 HTTP {exc.code}: {exc.reason}\nURL: {url}"
        ) from exc

    result = json.loads(raw)
    if isinstance(result.get("data"), str):
        try:
            result["data"] = json.loads(result["data"])
        except (json.JSONDecodeError, TypeError):
            pass

    return result


def _rows(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从 {data:{page:{rows:[]}}} 或 {data:{rows:[]}} 中提取行列表。"""
    data = resp.get("data") or {}
    if isinstance(data, dict):
        page = data.get("page")
        if isinstance(page, dict) and isinstance(page.get("rows"), list):
            return page["rows"]
        if isinstance(data.get("rows"), list):
            return data["rows"]
        if isinstance(data, list):
            return data
    elif isinstance(data, list):
        return data
    return []


def _total_size(resp: Dict[str, Any]) -> int:
    """读取总条数，兼容 ``data.page.totalSize`` 与 ``data.totalSize``。

    ⚠️ 商品接口 ``product/pageList`` 的 totalSize / totalPage **恒为 0**
    （即使 rows 有数据），因此分页判断不能依赖它，只能靠「本页行数 < pageSize」。
    广告接口 ``campaign/getAllCampaignData`` 的 totalSize 是可靠的。
    """
    data = resp.get("data") or {}
    if not isinstance(data, dict):
        return 0
    page = data.get("page")
    if isinstance(page, dict):
        try:
            return int(page.get("totalSize") or 0)
        except (TypeError, ValueError):
            pass
    try:
        return int(data.get("totalSize") or 0)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# 店铺自动发现（v2.1）
#   —— 赛狐没有独立的「店铺列表」接口（常见路径全部 404），
#      但广告活动接口的每行都带 ``shopId`` + ``shopName``，
#      翻几页即可拿到账号下所有「有广告活动」的店铺；
#      再用商品接口补一轮「有商品但没广告」的店铺（只有 id，用占位名）。
# ---------------------------------------------------------------------------
def discover_shops(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    max_pages: int = 12,
    page_size: int = 200,
) -> List[Dict[str, str]]:
    """自动发现当前 Cookie 账号下的全部店铺。

    返回 ``[{"shop_id": "563540", "shop_name": "兜兜-JP"}, ...]``
    """
    today = datetime.date.today()
    if not end_date:
        end_date = (today - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    if not start_date:
        start_date = (today - datetime.timedelta(days=30)).strftime("%Y-%m-%d")

    found: Dict[str, str] = {}

    # 1) 广告活动接口：拿 shopId + shopName（主来源）
    for page in range(1, max_pages + 1):
        body = {
            "shopIdList": [],          # 空 = 全账号
            "adType": "sp",
            "statusList": ["enabled", "paused", "archived"],
            "startDate": start_date,
            "endDate": end_date,
            "orderField": "adCost",
            "orderType": "desc",
            "pageNo": page,
            "pageSize": page_size,
            "fromSingle": True,
        }
        resp = post_json("campaign/getAllCampaignData", body)
        rows = _rows(resp)
        if not rows:
            break
        for r in rows:
            sid = r.get("shopId")
            if sid:
                key = str(sid)
                name = str(r.get("shopName") or "").strip()
                if key not in found or (name and not found[key]):
                    found[key] = name

        total = _total_size(resp)
        if total and page * page_size >= total:
            break
        if len(rows) < page_size:
            break
        time.sleep(0.3)

    # 2) 商品接口兜底：补充「有商品但无广告活动」的店铺（无店铺名）
    try:
        body = dict(PRODUCT_BASE_PARAMS)
        body["shopIds"] = ""
        body["pageSize"] = "200"
        body["pageNo"] = "1"
        resp = post_form(
            "https://www.sellfox.com/api/gw/sellfox/sellfox-product/sellfox/product/pageList",
            body,
        )
        for r in _rows(resp):
            sid = r.get("shopId")
            if sid:
                found.setdefault(str(sid), "")
    except Exception:
        # 兜底失败不影响主结果
        pass

    return [
        {"shop_id": sid, "shop_name": name or f"店铺{sid}"}
        for sid, name in sorted(found.items(), key=lambda kv: kv[0])
    ]


# ---------------------------------------------------------------------------
# 商品列表接口（指南 3.1 节）
# ---------------------------------------------------------------------------
def fetch_product_page(
    shop_id: str = "",
    page_no: int = 1,
    page_size: int = 200,
    search_value: str = "",
) -> Dict[str, Any]:
    """拉取一页在线商品数据（form 编码）。

    ``shop_id`` 传空字符串表示**全账号**（不限店铺）。
    返回原始响应，含 ``data.rows``（注意：totalSize 恒为 0，不可用于分页判断）。
    """
    body = dict(PRODUCT_BASE_PARAMS)
    body["shopIds"] = shop_id
    body["pageNo"] = str(page_no)
    body["pageSize"] = str(page_size)
    body["searchValue"] = search_value

    return post_form(
        "https://www.sellfox.com/api/gw/sellfox/sellfox-product/sellfox/product/pageList",
        body,
    )


def fetch_all_products(
    shop_id: str = "",
    page_size: int = 200,
    max_pages: int = 60,
) -> List[Dict[str, Any]]:
    """分页拉取全店在线商品（``shop_id`` 为空则拉全账号）。

    对应后台路径：销售 → 在线产品 → 父体/子体 → 选择店铺 → 导出核心字段。

    ⚠️ 分页停止条件：**只能**看「本页行数 < page_size」。
    商品接口的 ``totalSize`` / ``totalPage`` 恒为 0（实测 50 行数据仍是 0），
    若按 totalSize 判断会在第一页就 break，导致只抓到 200 条。
    """
    all_rows: List[Dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        resp = fetch_product_page(shop_id, page_no=page, page_size=page_size)
        rows = _rows(resp)
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < page_size:
            break
        time.sleep(0.3)  # 礼貌延迟

    return all_rows


def parse_product_row(row: Dict[str, Any], shop_id: str, shop_name: str) -> Dict[str, Any]:
    """把赛狐商品接口返回行映射到本地 products 表字段。

    实测字段（2026-11，日本站）：
    ``asin`` / ``parentAsin`` / ``parentSku`` / ``sku`` / ``title`` /
    ``mainImage`` / ``standardPrice`` / ``landedPrice`` / ``dxmState`` /
    ``onlineStatus`` / ``isVariation``（2 = 子体）
    """
    parent_asin = row.get("parentAsin") or ""
    asin = row.get("asin") or ""
    parent_sku_raw = row.get("parentSku") or ""
    sku_raw = row.get("sku") or ""

    # 类型判定：有 parentSku 且与自身 sku 不同 → 子体
    if parent_sku_raw and sku_raw and parent_sku_raw != sku_raw:
        item_type = "子体"
    elif parent_asin and asin and parent_asin != asin:
        item_type = "子体"
    elif parent_asin and (not asin or parent_asin == asin):
        item_type = "父体"
    else:
        item_type = "单体"

    return {
        "shop_id": shop_id,
        "shop_name": shop_name,
        "item_type": item_type,
        "parent_asin": parent_asin,
        "asin": asin,
        "msku": sku_raw or parent_sku_raw,
        "sku": sku_raw,
        # 父 SKU 优先取接口的 parentSku（如 HUA226），由调用方再做前缀清洗
        "parent_sku": parent_sku_raw,
        "raw_title": row.get("title") or "",
        "clean_zh_name": "",  # 由调用方经过名词词库匹配后填充
        "price": float(row.get("standardPrice") or row.get("landedPrice") or 0),
        "image_url": (
            row.get("mainImage")
            or row.get("mainBigImage")
            or row.get("imageUrl")
            or ""
        ),
        "online_status": row.get("dxmState") or row.get("onlineStatus") or "",
    }


# ---------------------------------------------------------------------------
# 广告数据接口（指南 1.x 节）
# ---------------------------------------------------------------------------
def fetch_all_campaigns(
    shop_id: str,
    start_date: str,
    end_date: str,
    status_list: Optional[List[str]] = None,
    page_size: int = 200,
    max_pages: int = 80,
) -> List[Dict[str, Any]]:
    """获取指定店铺在日期区间内的**全部**广告活动（自动翻页）。

    对应接口：``POST campaign/getAllCampaignData``

    ⚠️ 该接口 totalSize 可靠（实测单店单日可达数百条），必须翻页；
    只取第一页会导致大量活动漏抓、搜索词覆盖率严重不足。
    """
    all_rows: List[Dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        body = {
            "shopIdList": [shop_id] if shop_id else [],
            "adType": "sp",
            "statusList": status_list or ["enabled", "paused"],
            "startDate": start_date,
            "endDate": end_date,
            "orderField": "adCost",
            "orderType": "desc",
            "pageNo": page,
            "pageSize": page_size,
            "fromSingle": True,
        }
        resp = post_json("campaign/getAllCampaignData", body)
        rows = _rows(resp)
        if not rows:
            break
        all_rows.extend(rows)

        total = _total_size(resp)
        if total and len(all_rows) >= total:
            break
        if len(rows) < page_size:
            break
        time.sleep(0.25)

    return all_rows


def fetch_ad_products(
    shop_id: str,
    campaign_ids: List[str],
    start_date: str,
    end_date: str,
    page_size: int = 500,
    max_pages: int = 20,
) -> List[Dict[str, Any]]:
    """获取活动下关联的广告产品（自动翻页）。

    对应接口：``POST multiple/adProduct/getAdProductList``
    建立 campaignId → SKU/ASIN 的映射关系。
    """
    all_rows: List[Dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        body = {
            "type": "sp",
            "campaignIdList": campaign_ids,
            "shopIdList": [shop_id],
            "statusList": ["enabled", "paused"],
            "startDate": start_date,
            "endDate": end_date,
            "pageNo": page,
            "pageSize": page_size,
            "orderBy": "adCost",
            "desc": True,
            "searchContents": [],
            "searchMode": "exact",
            "searchType": "asin",
            "adStrategyTypeList": [],
            "servingStatusList": [],
            "useAdvanced": False,
        }
        resp = post_json("multiple/adProduct/getAdProductList", body)
        rows = _rows(resp)
        if not rows:
            break
        all_rows.extend(rows)

        total = _total_size(resp)
        if total and len(all_rows) >= total:
            break
        if len(rows) < page_size:
            break
        time.sleep(0.2)

    return all_rows


def fetch_search_terms(
    shop_id: str,
    campaign_ids: List[str],
    start_date: str,
    end_date: str,
    query_type: str = "searchKeyword",
    page_size: int = 500,
    max_pages: int = 20,
) -> List[Dict[str, Any]]:
    """拉取搜索词维度数据（自动翻页）。

    对应接口：``POST multiple/search/getAllSearchData``
    参数 ``searchQueryType = "searchKeyword"`` 获取搜索词维度。

    返回行字段：``query``(搜索词), ``matchType``, ``impressions``, ``clicks``,
    ``adCost``, ``adOrderNum``, ``adSale``, ``acos``, ``cvr``,
    ``adCostPerClick``(CPC), ``suggested``(建议竞价), ``asin``, ``msku`` 等。
    """
    all_rows: List[Dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        body = {
            "orderField": "impressions",
            "orderType": "desc",
            "shopIdList": [shop_id],
            "campaignIdList": campaign_ids,
            "adType": "sp",
            "searchQueryType": query_type,
            "statusList": ["enabled", "paused"],
            "servingStatusList": [],
            "startDate": start_date,
            "endDate": end_date,
            "pageNo": page,
            "pageSize": page_size,
            "fromSingle": True,
            "useAdvanced": False,
        }
        resp = post_json("multiple/search/getAllSearchData", body)
        rows = _rows(resp)
        if not rows:
            break
        all_rows.extend(rows)

        total = _total_size(resp)
        if total and len(all_rows) >= total:
            break
        if len(rows) < page_size:
            break
        time.sleep(0.2)

    return all_rows


def fetch_suggested_bid(
    shop_id: str, target_ids: List[str]
) -> List[Dict[str, Any]]:
    """获取投放建议竞价。

    对应接口：``POST spTarget/getSuggestedBid``
    注意：``targetIds`` 必须是逗号字符串，必须是 Amazon targetId。
    """
    body = {
        "shopId": shop_id,
        "targetIds": ",".join(target_ids),
    }
    resp = post_json("spTarget/getSuggestedBid", body)
    data = resp.get("data")
    if isinstance(data, list):
        return data
    return []


# ---------------------------------------------------------------------------
# SP 广告创建接口（sellfox_sp_create_api.md 第 2 节）
# ---------------------------------------------------------------------------
def create_sp_ad(payload: Dict[str, Any]) -> Dict[str, Any]:
    """创建 SP 广告活动（通用入口）。

    对应接口：``POST ad/sp/create``
    - JSON 请求体，三段结构：spCampaignInfo + spGroupInfo + spTargetInfo
    - 鉴权：仅 Cookie，不需要签名头
    - 非幂等：每次调用都新建

    返回响应体，调用方需检查根 ``code=0`` 且各子模块 ``Resp.code=0``。
    """
    return post_json("ad/sp/create", payload)


def build_auto_ad_payload(
    shop_id: str,
    campaign_name: str,
    daily_budget: str = "100",
    start_date: str = "",
    end_date: str = "",
    portfolio_id: str = "",
    strategy: str = "legacyForSales",
    default_bid: str = "25",
    bid_close: str = "25",
    bid_loose: str = "20",
    bid_sub: str = "25",
    bid_comp: str = "20",
    products: Optional[List[Dict[str, Any]]] = None,
    placement_top: Optional[str] = None,
    placement_product_page: Optional[str] = None,
    placement_rest_of_search: Optional[str] = None,
) -> Dict[str, Any]:
    """构建自动投放 SP 广告的请求体。

    ``strategy`` 枚举：
    - ``autoForSales`` — 动态竞价-提高和降低
    - ``legacyForSales`` — 动态竞价-只降低
    - ``manual`` — 固定竞价
    """
    today = start_date or time.strftime("%Y-%m-%d")
    return {
        "shopId": shop_id,
        "marketplaceId": JP_MARKETPLACE_ID,
        "spCampaignInfo": {
            "campaignId": "",
            "startDateStr": today,
            "endDateStr": end_date,
            "dailyBudget": str(daily_budget),
            "targetingType": "auto",
            "strategy": strategy,
            "placementTop": placement_top,
            "placementProductPage": placement_product_page,
            "placementRestOfSearch": placement_rest_of_search,
            "placementSiteAmazonBusiness": None,
            "creativeBidPercentage": None,
            "campaignSite": "amazonAll",
            "name": campaign_name,
            "portfolioId": portfolio_id,
            "audienceId": "",
            "audienceBidPercentage": "",
            "defaultAudience": False,
        },
        "spGroupInfo": {
            "groupId": "",
            "defaultBid": str(default_bid),
            "name": campaign_name,
            "products": products or [],
        },
        "spTargetInfo": {
            "autoTarget": True,
            "keywordTarget": False,
            "queryHighRelMatchesState": True,
            "queryHighRelMatchesBid": str(bid_close),
            "queryBroadRelMatchesState": True,
            "queryBroadRelMatchesBid": str(bid_loose),
            "asinSubstituteRelatedState": True,
            "asinSubstituteRelatedBid": str(bid_sub),
            "asinAccessoryRelatedState": True,
            "asinAccessoryRelatedBid": str(bid_comp),
            "keywords": [],
            "targetings": [],
            "neKeywords": [],
            "neTargetings": [],
        },
    }


def build_manual_ad_payload(
    shop_id: str,
    campaign_name: str,
    group_name: str,
    daily_budget: str = "100",
    start_date: str = "",
    end_date: str = "",
    portfolio_id: str = "",
    strategy: str = "legacyForSales",
    default_bid: str = "25",
    keywords: Optional[List[Dict[str, Any]]] = None,
    products: Optional[List[Dict[str, Any]]] = None,
    placement_top: Optional[str] = None,
    placement_product_page: Optional[str] = None,
    placement_rest_of_search: Optional[str] = None,
) -> Dict[str, Any]:
    """构建手动关键词投放 SP 广告的请求体。

    ``keywords`` 格式：``[{"keywordText": "dog cage", "matchType": "exact", "bid": "15"}]``
    ``matchType`` 枚举：``exact`` / ``phrase`` / ``broad`` / ``theme``
    """
    today = start_date or time.strftime("%Y-%m-%d")
    return {
        "shopId": shop_id,
        "marketplaceId": JP_MARKETPLACE_ID,
        "spCampaignInfo": {
            "campaignId": "",
            "startDateStr": today,
            "endDateStr": end_date,
            "dailyBudget": str(daily_budget),
            "targetingType": "manual",
            "strategy": strategy,
            "placementTop": placement_top,
            "placementProductPage": placement_product_page,
            "placementRestOfSearch": placement_rest_of_search,
            "placementSiteAmazonBusiness": None,
            "creativeBidPercentage": None,
            "campaignSite": "amazonAll",
            "name": campaign_name,
            "portfolioId": portfolio_id,
            "audienceId": "",
            "audienceBidPercentage": "",
            "defaultAudience": False,
        },
        "spGroupInfo": {
            "groupId": "",
            "defaultBid": str(default_bid),
            "name": group_name,
            "products": products or [],
        },
        "spTargetInfo": {
            "autoTarget": False,
            "keywordTarget": True,
            "queryHighRelMatchesState": True,
            "queryHighRelMatchesBid": "",
            "queryBroadRelMatchesState": True,
            "queryBroadRelMatchesBid": "",
            "asinSubstituteRelatedState": True,
            "asinSubstituteRelatedBid": "",
            "asinAccessoryRelatedState": True,
            "asinAccessoryRelatedBid": "",
            "keywords": keywords or [],
            "targetings": [],
            "neKeywords": [],
            "neTargetings": [],
        },
    }


def check_create_result(resp: Dict[str, Any]) -> Dict[str, Any]:
    """检查创建结果，返回结构化的成功/失败信息。

    判读规则：根 ``code=0`` 且各子模块 ``Resp.code=0`` 才算对应部分成功。
    """
    root_code = resp.get("code", -1)
    root_msg = resp.get("msg", "")
    data = resp.get("data") or {}

    result = {
        "success": root_code == 0,
        "root_code": root_code,
        "root_msg": root_msg,
        "campaign_id": "",
        "ad_group_id": "",
        "target_count": 0,
        "target_success": 0,
        "errors": [],
    }

    if root_code != 0:
        result["errors"].append(f"根响应 code={root_code}, msg={root_msg}")
        if root_code == 2001:
            result["errors"].append("Cookie 过期，请重新获取登录态")
        return result

    # 检查各子模块
    campaign_resp = data.get("campaignResp") or {}
    group_resp = data.get("groupResp") or {}
    target_resp = data.get("targetResp") or {}

    if campaign_resp.get("code", -1) != 0:
        result["errors"].append(
            f"活动创建失败: code={campaign_resp.get('code')}, msg={campaign_resp.get('errMsg', '')}"
        )
    else:
        result["campaign_id"] = campaign_resp.get("campaignId", "")

    if group_resp.get("code", -1) != 0:
        result["errors"].append(
            f"广告组创建失败: code={group_resp.get('code')}, msg={group_resp.get('errMsg', '')}"
        )
    else:
        result["ad_group_id"] = group_resp.get("adGroupId", "")

    target_list = target_resp.get("targetList") or []
    result["target_count"] = len(target_list)
    for i, t in enumerate(target_list):
        if t.get("code", -1) != 0:
            result["errors"].append(
                f"投放 {i} 创建失败: code={t.get('code')}, msg={t.get('errMsg', '')}"
            )
        else:
            result["target_success"] += 1

    result["success"] = len(result["errors"]) == 0
    return result


# ---------------------------------------------------------------------------
# 批量创建：1 活动 + N 广告组（sellfox_sp_create_api.md 第 11 节）
# ---------------------------------------------------------------------------
def create_campaign_with_multiple_groups(
    shop_id: str,
    campaign_name: str,
    groups: List[Dict[str, Any]],
    daily_budget: str = "300",
    start_date: str = "",
    end_date: str = "",
    portfolio_id: str = "",
    strategy: str = "legacyForSales",
    interval_sec: float = 2.0,
) -> List[Dict[str, Any]]:
    """创建 1 个活动 + N 个广告组（第 1 次建活动+组1，后续追加组）。

    ``groups`` 每项格式：``{"name": "GR1", "default_bid": "20", "products": [...], "keywords": [...]}``

    返回每个组的创建结果列表。
    """
    results: List[Dict[str, Any]] = []
    campaign_id = ""

    for i, g in enumerate(groups):
        if i == 0:
            # 第 1 次：建活动 + 第 1 组
            payload = build_auto_ad_payload(
                shop_id=shop_id,
                campaign_name=campaign_name,
                daily_budget=daily_budget,
                start_date=start_date,
                end_date=end_date,
                portfolio_id=portfolio_id,
                strategy=strategy,
                default_bid=g.get("default_bid", "25"),
                products=g.get("products", []),
            )
        else:
            # 后续：追加组（campaignId 填已有活动 ID）
            today = start_date or time.strftime("%Y-%m-%d")
            payload = {
                "shopId": shop_id,
                "marketplaceId": JP_MARKETPLACE_ID,
                "spCampaignInfo": {
                    "campaignId": campaign_id,
                    "startDateStr": "",
                    "endDateStr": "",
                    "dailyBudget": str(daily_budget),
                    "targetingType": "auto",
                    "strategy": strategy,
                    "placementTop": None,
                    "placementProductPage": None,
                    "placementRestOfSearch": None,
                    "placementSiteAmazonBusiness": None,
                    "creativeBidPercentage": None,
                    "campaignSite": "amazonAll",
                    "name": "",
                    "portfolioId": "",
                    "audienceId": "",
                    "audienceBidPercentage": "",
                    "defaultAudience": False,
                    "offAmazonBudgetControlStrategy": None,
                },
                "spGroupInfo": {
                    "groupId": "",
                    "defaultBid": str(g.get("default_bid", "25")),
                    "name": g.get("name", f"GR_{i+1}"),
                    "products": g.get("products", []),
                },
                "spTargetInfo": {
                    "autoTarget": True,
                    "keywordTarget": False,
                    "queryHighRelMatchesState": True,
                    "queryHighRelMatchesBid": str(g.get("bid_close", "25")),
                    "queryBroadRelMatchesState": True,
                    "queryBroadRelMatchesBid": str(g.get("bid_loose", "20")),
                    "asinSubstituteRelatedState": True,
                    "asinSubstituteRelatedBid": str(g.get("bid_sub", "25")),
                    "asinAccessoryRelatedState": True,
                    "asinAccessoryRelatedBid": str(g.get("bid_comp", "20")),
                    "keywords": [],
                    "targetings": [],
                    "neKeywords": [],
                    "neTargetings": [],
                },
            }

        resp = create_sp_ad(payload)
        check_result = check_create_result(resp)
        check_result["group_name"] = g.get("name", f"GR_{i+1}")
        check_result["group_index"] = i

        if i == 0 and check_result["campaign_id"]:
            campaign_id = check_result["campaign_id"]

        results.append(check_result)

        if i < len(groups) - 1:
            time.sleep(interval_sec)

    return results


# ---------------------------------------------------------------------------
# 二次过滤校验（指南 0.3 节）
# ---------------------------------------------------------------------------
def filter_campaigns_by_status(
    campaigns: List[Dict[str, Any]],
    status_list: List[str],
) -> List[Dict[str, Any]]:
    """二次过滤活动列表（接口 statusList 不严格生效）。"""
    if not status_list:
        return campaigns
    return [c for c in campaigns if c.get("state") in status_list]


def filter_search_terms_by_date(
    rows: List[Dict[str, Any]],
    target_date: str,
) -> List[Dict[str, Any]]:
    """二次过滤搜索词数据（确保日期精确匹配）。"""
    return [r for r in rows if r.get("reportDate") == target_date or not target_date]
