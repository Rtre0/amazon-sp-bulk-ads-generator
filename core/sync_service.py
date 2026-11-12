"""数据同步服务。

两大核心任务：
1. **商品每周增量同步**：拉取赛狐在线商品 → 本地名词词库清洗 → 增量入库（跳过已存在）
2. **搜索词每日凌晨同步**：按店铺分片拉取双维度日报 → 广告产品映射 → 入库

设计要点
--------
- 商品同步以 ``(shop_id, msku, asin)`` 唯一键去重，已存在的旧商品直接丢弃，
  不覆盖用户已微调过的中文品名；仅入库新上架商品。
- 搜索词同步以 ``(report_date, shop_id, search_term, campaign_id, match_type)`` 去重，
  重复同步会更新指标（赛狐会回溯修正历史数据）。
- **⚠️ 搜索词必须按店铺分片拉取，不能一次拉全账号**：
  赛狐 ``getAllSearchData`` 的 pageSize 上限被服务端锁死在 500，且按 impressions
  排序不稳定，全账号一次拉需要翻页，会随机漏掉 5%~19% 的行。
  实测 2026-11-07：分片 6 店合计 725 行 = 接口 total，两次拉取完全一致；
  全账号翻页只拿到 601 个唯一行。单店单日 80~260 行，不翻页即完整。
- 两个维度分别抓：``searchKeyword``（用户搜索词）、``searchAsin``（投放产生 ASIN）。
- 当前 Cookie 账号无权限的店铺会明确报错，不会静默跳过。
- 所有同步任务都通过本模块统一调度，前端也可手动触发。
"""

from __future__ import annotations

import datetime
import time
from typing import Any, Dict, List, Optional

from core import database as db
from core import sellfox_client as sf
from core.parser import clean_parent_sku, clean_product_name
from core.translator import noun_matcher

# 单次请求携带的 campaignId 上限。
# 一个店铺可能有数百个活动，一次性塞进请求体会超限/超时，必须分批。
CAMPAIGN_BATCH_SIZE = 50


def _to_float(value: Any, default: float = 0.0) -> float:
    """安全转 float（接口里数值字段常是字符串，如 ``'250.86'``）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 商品增量同步
# ---------------------------------------------------------------------------
def _insert_product_rows(
    rows: List[Dict[str, Any]], shop_id: str, shop_name: str
) -> Dict[str, int]:
    """把一批商品行清洗后增量入库，返回 ``{new_inserted, skipped}``。

    增量去重规则：``(shop_id, msku, asin)`` 已存在则跳过，
    **不覆盖用户已微调过的中文品名**。
    """
    new_count = 0
    skipped_count = 0

    for row in rows:
        parsed = sf.parse_product_row(row, shop_id, shop_name)

        # 父 SKU：优先用接口 parentSku（HUA226），再回落到 sku / asin 做前缀清洗
        parent_raw = parsed.get("parent_sku") or parsed.get("msku") or parsed.get("asin") or ""
        parsed["parent_sku"] = clean_parent_sku(parent_raw) or parent_raw.lower()

        # 名词词库匹配生成中文品名
        raw_title = parsed.get("raw_title") or ""
        if raw_title:
            zh_name = noun_matcher.match_first_noun(raw_title)
            parsed["clean_zh_name"] = zh_name or clean_product_name(raw_title)
        else:
            parsed["clean_zh_name"] = "商品"

        inserted = db.insert_product(
            shop_id=shop_id,
            shop_name=shop_name,
            item_type=parsed["item_type"],
            parent_asin=parsed["parent_asin"],
            asin=parsed["asin"],
            msku=parsed["msku"],
            sku=parsed["sku"],
            parent_sku=parsed["parent_sku"],
            raw_title=parsed["raw_title"],
            clean_zh_name=parsed["clean_zh_name"],
            price=parsed["price"],
            image_url=parsed["image_url"],
            online_status=parsed["online_status"],
        )
        if inserted:
            new_count += 1
        else:
            skipped_count += 1

    return {"new_inserted": new_count, "skipped": skipped_count}


def discover_and_register_shops(account_id: str = "") -> List[Dict[str, Any]]:
    """自动发现**当前账号**下全部店铺并写入 shops 表。

    赛狐没有独立店铺列表接口，改为从广告活动接口的 ``shopId`` / ``shopName``
    反推（详见 ``sellfox_client.discover_shops``）。

    多账号场景下会把店铺归属（``account_id``）一起记下来，
    同时写回账号池，前端即可按账号分组展示。
    """
    acc = sf.get_account(account_id or None)
    aid = (acc or {}).get("id", "") or account_id
    discovered = sf.discover_shops()
    for d in discovered:
        db.upsert_shop(d["shop_id"], d["shop_name"], account_id=aid)
    if aid:
        sf.update_account_shops(aid, discovered)
    return discovered


def sync_products_for_shop(
    shop_id: str,
    shop_name: str = "",
    marketplace: str = "A1VC38T7YXB528",
) -> Dict[str, Any]:
    """拉取**单个店铺**的全量在线商品并增量入库。"""
    acc = sf.get_account()
    db.upsert_shop(
        shop_id, shop_name, marketplace,
        account_id=(acc or {}).get("id", ""),
    )

    all_rows = sf.fetch_all_products(shop_id)
    stat = _insert_product_rows(all_rows, shop_id, shop_name)
    db.update_shop_sync_time(shop_id, "product")

    return {
        "shop_id": shop_id,
        "shop_name": shop_name,
        "total_fetched": len(all_rows),
        **stat,
    }


def sync_products_all_shops(account_id: str = "") -> List[Dict[str, Any]]:
    """同步**当前账号**下全部店铺的商品（逐店铺拉取）。

    ⚠️ 为什么逐店铺拉而不是「一次拉全账号再分组」：
    商品接口的排序是固定的（按店铺聚集），单店商品可达数千条。
    一次拉全账号会在 ``max_pages`` 处截断，排在后面的店铺**完全抓不到**
    （实测 5 店铺时只抓到前 2 个）。逐店铺拉可保证每家店都翻到底。

    ⚠️ 多账号场景必须按「当前账号可访问店铺」过滤：
    ``shops`` 表是全局的，直接遍历会把别的账号的店铺也拉一遍，
    结果全是「没有店铺访问权限」。
    """
    acc = sf.get_account(account_id or None)
    aid = (acc or {}).get("id", "") or account_id

    # 1) 先探测当前账号能访问的店铺，顺带把归属写进 shops 表
    try:
        accessible = discover_and_register_shops(aid)
    except Exception as exc:  # noqa: BLE001
        accessible = []
        discover_error = str(exc)
    else:
        discover_error = ""

    if accessible:
        allowed = {str(d["shop_id"]) for d in accessible}
        shops = [
            s for s in (db.list_shops(aid) if aid else db.list_shops())
            if str(s["shop_id"]) in allowed
        ]
        if not shops:   # 库里有归属缺失时，直接用接口结果兜底
            shops = [
                {"shop_id": d["shop_id"], "shop_name": d["shop_name"]}
                for d in accessible
            ]
    else:
        shops = db.list_shops(aid) if aid else db.list_shops()
        if not shops:
            return [{
                "shop_id": "-",
                "shop_name": "-",
                "error": (
                    "未能发现任何店铺：账号下没有广告活动或商品，"
                    "请先在赛狐后台确认店铺已授权"
                    + (f"（探测失败：{discover_error}）" if discover_error else "")
                ),
            }]

    results: List[Dict[str, Any]] = []

    for shop in shops:
        sid = str(shop["shop_id"])
        name = shop.get("shop_name") or f"店铺{sid}"
        try:
            rows = sf.fetch_all_products(sid)
            stat = _insert_product_rows(rows, sid, name)
            db.update_shop_sync_time(sid, "product")
            results.append({
                "shop_id": sid,
                "shop_name": name,
                "total_fetched": len(rows),
                **stat,
            })
        except Exception as exc:  # noqa: BLE001 - 单店失败不拖垮其他店
            results.append({
                "shop_id": sid,
                "shop_name": name,
                "error": str(exc),
            })

    return results


# ---------------------------------------------------------------------------
# 搜索词每日同步
# ---------------------------------------------------------------------------
def _yesterday() -> str:
    """返回昨天的日期字符串 (YYYY-MM-DD)。"""
    return (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")


def sync_search_terms_for_shop(
    shop_id: str,
    shop_name: str = "",
    target_date: Optional[str] = None,
    prefetched=None,
    sharded: bool = False,
) -> Dict[str, Any]:
    """拉取指定店铺昨日的搜索词数据并入库。

    数据抓取链路（指南 2.1 节）：
    1. ``campaign/getAllCampaignData`` 获取昨日所有活动列表
    2. ``multiple/adProduct/getAdProductList`` 获取活动关联的广告产品
    3. ``multiple/search/getAllSearchData`` 拉取搜索词维度数据
    4. 二次过滤校验后入库

    Args:
        shop_id: 店铺 ID
        shop_name: 店铺名称
        target_date: 目标日期 (YYYY-MM-DD)，默认昨天
    """
    if not target_date:
        target_date = _yesterday()

    # 确保店铺在库
    if not db.get_shop(shop_id):
        db.upsert_shop(shop_id, shop_name)

    # v2.3：**必须按店铺分片拉取**。
    # 赛狐 getAllSearchData 的 pageSize 上限被服务端锁死在 500，且按 impressions
    # 排序不稳定：全账号一次拉需要翻页，同一行会重复出现在多页、另一些真实行被挤掉。
    # 实测 2026-11-07 全账号 total=725，按店铺分片 6 店合计正好 725 行且两次一致；
    # 而全账号翻页一次只拿到 601 个唯一行（漏 17%），这正是「少几个搜索词」的根因。
    # 单店单日通常 80~260 行 < 500，一次拉完不翻页，从根上消除漏行。
    #
    # 两个维度都要抓：searchKeyword（用户搜索词）+ searchAsin（投放产生 ASIN）。
    if prefetched is not None:
        # 全账号同步时复用同一批数据，避免每个店铺重复发起请求
        kw_all, asin_all = prefetched
    else:
        kw_all = sf.fetch_search_terms(
            shop_id=shop_id,
            campaign_ids=[],          # 空 = 该店铺全部活动
            start_date=target_date,
            end_date=target_date,
            query_type="searchKeyword",
        )
        asin_all = sf.fetch_search_terms(
            shop_id=shop_id,
            campaign_ids=[],
            start_date=target_date,
            end_date=target_date,
            query_type="searchAsin",
        )

    accessible = sorted({
        str(r.get("shopId")) for r in (kw_all + asin_all) if r.get("shopId")
    })
    if prefetched is not None and not sharded:
        # 兼容旧调用：整批是全账号数据时按行内 shopId 过滤
        search_rows = [r for r in kw_all if str(r.get("shopId")) == str(shop_id)]
        asin_rows = [r for r in asin_all if str(r.get("shopId")) == str(shop_id)]
    else:
        # 已按店铺分片（或本函数自己按 shop_id 拉取），整批就是该店铺数据
        search_rows = kw_all
        asin_rows = asin_all

    if not search_rows and not asin_rows:
        hint = ""
        if accessible and str(shop_id) not in accessible:
            names = sorted({
                str(r.get("shopName")) for r in (kw_all + asin_all)
                if r.get("shopName")
            })
            hint = (
                f"当前 Cookie 账号可访问的店铺是：{', '.join(names) or accessible}；"
                "不含本店铺。请在「同步与 Cookie 设置」重新粘贴该店铺所属赛狐账号的 Cookie。"
            )
        return {
            "shop_id": shop_id,
            "shop_name": shop_name,
            "report_date": target_date,
            "campaigns": 0,
            "active_campaigns": 0,
            "search_terms_fetched": 0,
            "search_terms_inserted": 0,
            "orders_found": 0,
            "accessible_shops": accessible,
            "message": hint or "该日无搜索词数据（可能无曝光/花费）",
            "error": hint or "",
        }

    # 从搜索词行反推涉及的活动 ID（只对这些活动查广告产品做 ASIN/SKU 映射）
    campaign_ids = sorted({
        str(r.get("campaignId")) for r in (search_rows + asin_rows) if r.get("campaignId")
    })

    # 活动 ID 分批：单店铺可能有数百个活动，一次性塞进请求体会超限或超时
    batches = [
        campaign_ids[i : i + CAMPAIGN_BATCH_SIZE]
        for i in range(0, len(campaign_ids), CAMPAIGN_BATCH_SIZE)
    ]

    # 2. 获取广告产品（建立 campaignId → SKU/ASIN 映射）
    campaign_product_map: Dict[str, List[Dict[str, Any]]] = {}
    for batch in batches:
        try:
            ad_products = sf.fetch_ad_products(
                shop_id=shop_id,
                campaign_ids=batch,
                start_date=target_date,
                end_date=target_date,
            )
        except Exception:
            ad_products = []
        for ap in ad_products:
            cid = ap.get("campaignId") or ""
            campaign_product_map.setdefault(cid, []).append(ap)
        time.sleep(0.1)

    # 4. 二次过滤 + 映射字段 + 入库
    db_rows: List[Dict[str, Any]] = []
    for r in search_rows:
        search_term = (r.get("query") or "").strip()
        if not search_term:
            continue  # 空搜索词无意义

        row_date = r.get("reportDate") or target_date
        cid = r.get("campaignId") or ""

        # 从广告产品映射中找 ASIN/MSKU（搜索词行本身不带商品标识）
        asin = r.get("asin") or ""
        msku = r.get("msku") or ""
        if not asin or not msku:
            products = campaign_product_map.get(cid, [])
            if products:
                asin = asin or products[0].get("asin") or ""
                msku = msku or products[0].get("sku") or ""

        db_row = {
            "report_date": row_date,
            "shop_id": shop_id,
            "shop_name": shop_name,
            "campaign_id": cid,
            # 接口行内自带活动名/广告组名，直接取，无需回查活动列表
            "campaign_name": r.get("campaignName") or "",
            "group_id": r.get("adGroupId") or "",
            "group_name": r.get("adGroupName") or "",
            "search_term": search_term,
            "match_type": r.get("matchType") or "",
            "impressions": r.get("impressions") or 0,
            "clicks": r.get("clicks") or 0,
            "ctr": r.get("ctr"),
            "ad_cost": r.get("adCost") or 0,
            "cpc": r.get("adCostPerClick"),
            "ad_orders": r.get("adOrderNum") or 0,
            "ad_sales": r.get("adSale") or 0,
            "cvr": r.get("cvr"),
            "acos": r.get("acos"),
            "roas": r.get("roas"),
            "suggested_bid": r.get("suggested"),
            "asin": asin,
            "msku": msku,
        }
        db_rows.append(db_row)

    inserted = db.insert_search_terms_batch(db_rows)

    # 「投放产生 ASIN」维度：一行 = 一个被投放/被触达的商品页 ASIN
    asin_db_rows: List[Dict[str, Any]] = []
    for r in asin_rows:
        target_asin = str(r.get("query") or "").strip().upper()
        if not target_asin:
            continue
        cid = r.get("campaignId") or ""
        products = campaign_product_map.get(cid, [])
        asin_db_rows.append({
            "report_date": r.get("reportDate") or target_date,
            "shop_id": shop_id,
            "shop_name": shop_name,
            "campaign_id": cid,
            "campaign_name": r.get("campaignName") or "",
            "group_id": r.get("adGroupId") or "",
            "group_name": r.get("adGroupName") or "",
            "target_asin": target_asin,
            "targeting_expression": r.get("targetingExpression") or r.get("matchType") or "",
            "targeting_type": r.get("queryType") or "",
            "match_type": r.get("matchType") or "",
            "impressions": r.get("impressions") or 0,
            "clicks": r.get("clicks") or 0,
            "ctr": r.get("ctr"),
            "ad_cost": r.get("adCost") or 0,
            "cpc": r.get("adCostPerClick"),
            "ad_orders": r.get("adOrderNum") or 0,
            "ad_sales": r.get("adSale") or 0,
            "cvr": r.get("cvr"),
            "acos": r.get("acos"),
            "roas": r.get("roas"),
            "suggested_bid": r.get("suggested"),
            "price": r.get("price"),
            "asin_rating": r.get("asinRating"),
            "rating_count": r.get("ratingCount"),
            "asin": r.get("asin") or (products[0].get("asin") if products else "") or "",
            "msku": r.get("msku") or (products[0].get("sku") if products else "") or "",
        })
    asin_inserted = db.insert_search_asins_batch(asin_db_rows)

    db.update_shop_sync_time(shop_id, "search")

    return {
        "shop_id": shop_id,
        "shop_name": shop_name,
        "report_date": target_date,
        # 涉及的活动数（从搜索词行反推，非店铺活动总数）
        "campaigns": len(campaign_ids),
        "active_campaigns": len(campaign_ids),
        "search_terms_fetched": len(search_rows),
        "search_terms_inserted": inserted,
        "asin_rows_fetched": len(asin_rows),
        "asin_rows_inserted": asin_inserted,
        "orders_found": (
            sum(1 for r in db_rows if int(r.get("ad_orders") or 0) > 0)
            + sum(1 for r in asin_db_rows if int(r.get("ad_orders") or 0) > 0)
        ),
    }


def _discover_accessible_shops(dates: List[str]) -> List[Dict[str, str]]:
    """探测当前 Cookie 账号实际可访问的店铺。

    显式 ``shopIdList`` 传一个当前账号无权限的店铺会返回 ``360000 您没有店铺访问权限``，
    所以在「按店铺分片」之前必须先知道当前 Cookie 属于哪个账号。
    """
    start, end = min(dates), max(dates)
    try:
        found = sf.discover_shops(start_date=start, end_date=end)
    except Exception:  # noqa: BLE001 - 探测失败走兜底
        found = []
    if found:
        return [
            {"shop_id": str(x["shop_id"]), "shop_name": str(x.get("shop_name") or "")}
            for x in found
        ]

    # 兜底：用全账号搜索词第一页反推店铺 ID（不翻页，仅用于发现）
    try:
        probe = sf.fetch_search_terms(
            None, [], dates[-1], dates[-1], "searchKeyword", max_pages=1
        )
    except Exception:  # noqa: BLE001
        return []
    ids: Dict[str, str] = {}
    for r in probe:
        sid = str(r.get("shopId") or "")
        if sid:
            ids.setdefault(sid, str(r.get("shopName") or ""))
    return [{"shop_id": k, "shop_name": v} for k, v in ids.items()]


def sync_search_terms_all_shops(
    target_date: Optional[str] = None,
    days: int = 1,
    account_id: str = "",
) -> List[Dict[str, Any]]:
    """同步全账号店铺的搜索词数据。

    Args:
        target_date: 指定单日（YYYY-MM-DD）。给了就只同步这一天。
        days: 未指定 target_date 时，回补最近 N 天（1 = 仅昨天）。

    shops 表为空时自动先发现店铺（与商品同步保持一致）。

    为什么要「回补 N 天」：单日出单很少（实测某店 30 天仅 57 单），
    只看昨天常常一条出单词都没有。回补 7 天才能凑出足够的拓词样本。
    """
    if target_date:
        dates = [target_date]
    else:
        days = max(1, min(int(days or 1), 30))  # 夹取 1~30 天
        today = datetime.date.today()
        dates = [
            (today - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(1, days + 1)
        ]
    return _sync_search_terms_dates(dates, account_id)


def sync_search_terms_range(
    start_date: str,
    end_date: str,
    account_id: str = "",
) -> List[Dict[str, Any]]:
    """按**显式日期区间**补同步搜索词（含首尾两天）。

    与 ``sync_search_terms_all_shops`` 的唯一区别是日期从哪来：
    那边是「从昨天往前推 N 天」，这边是「你指哪段就补哪段」，
    专治历史缺口 —— 例如某店 11-01~11-03 从来没被跑过。

    仍然逐日、逐店、逐维度拉取：一次拉整个区间会翻页，赛狐排序不稳定会漏行。
    """
    try:
        d0 = datetime.date.fromisoformat(str(start_date).strip())
        d1 = datetime.date.fromisoformat(str(end_date).strip())
    except ValueError as exc:
        raise ValueError(f"日期格式应为 YYYY-MM-DD（{exc}）") from exc
    if d0 > d1:
        d0, d1 = d1, d0
    span = (d1 - d0).days + 1
    if span > 90:
        raise ValueError(f"单次最多补 90 天，当前 {span} 天")
    dates = [
        (d0 + datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(span)
    ]
    return _sync_search_terms_dates(dates, account_id)


def _sync_search_terms_dates(
    dates: List[str],
    account_id: str = "",
) -> List[Dict[str, Any]]:
    """按给定日期列表同步搜索词（逐日 × 逐店 × 双维度）。"""
    acc = sf.get_account(account_id or None)
    aid = (acc or {}).get("id", "") or account_id
    shops = db.list_shops(aid) if aid else db.list_shops()

    if not shops:
        try:
            discover_and_register_shops(aid)
        except Exception as exc:
            return [{
                "shop_id": "-",
                "shop_name": "-",
                "error": f"自动发现店铺失败（请检查 Cookie 是否过期）：{exc}",
            }]
        shops = db.list_shops(aid) if aid else db.list_shops()
        if not shops:
            return [{
                "shop_id": "-",
                "shop_name": "-",
                "error": "未能发现任何店铺：账号下没有广告活动或商品",
            }]

    results: List[Dict[str, Any]] = []
    registered = {str(s["shop_id"]): s["shop_name"] for s in shops}

    # 当前 Cookie 账号能访问的店铺（按店铺分片的前提）
    accessible = _discover_accessible_shops(dates)
    if not accessible:
        return [{
            "shop_id": "-", "shop_name": "-",
            "error": (
                "未能探测到当前 Cookie 账号下的店铺，请先在「同步与 Cookie 设置」"
                "重新粘贴赛狐登录态，并确认该账号有广告数据。"
            ),
        }]
    accessible_ids = {x["shop_id"] for x in accessible}
    for x in accessible:
        sid, name = x["shop_id"], x["shop_name"]
        db.upsert_shop(sid, name, account_id=aid)   # 补全/纠正店铺归属
        registered.setdefault(sid, name or registered.get(sid, ""))
    if aid:
        sf.update_account_shops(aid, accessible)

    permission_errors: List[str] = []
    for date_str in dates:
        # ⚠️ 逐店铺、逐维度拉取。绝不改成「不传 shopIdList 一次拉全账号」：
        # 那样会触发翻页，赛狐排序不稳定会随机漏掉 5%~19% 的行。
        for x in accessible:
            sid = x["shop_id"]
            name = registered.get(sid) or x["shop_name"]
            try:
                kw_rows = sf.fetch_search_terms(
                    sid, [], date_str, date_str, "searchKeyword"
                )
            except Exception as exc:  # noqa: BLE001 - 单店失败不拖垮其他店
                permission_errors.append(f"{name}({sid}) {date_str}: {exc}")
                continue
            try:
                asin_rows = sf.fetch_search_terms(
                    sid, [], date_str, date_str, "searchAsin"
                )
            except Exception as exc:  # noqa: BLE001 - ASIN 维度失败不丢关键词维度
                asin_rows = []
                results.append({
                    "shop_id": sid, "shop_name": name, "report_date": date_str,
                    "error": f"「投放产生 ASIN」维度抓取失败：{exc}",
                })

            try:
                results.append(sync_search_terms_for_shop(
                    sid, name, date_str,
                    prefetched=(kw_rows, asin_rows), sharded=True,
                ))
            except Exception as exc:
                results.append({
                    "shop_id": sid, "shop_name": name, "report_date": date_str,
                    "error": str(exc),
                })

    # 库里注册过、但当前 Cookie 账号无权访问的店铺：明确提示，不再静默跳过
    missing = [
        f"{registered[k]}({k})" for k in registered if k not in accessible_ids
    ]
    if missing:
        results.append({
            "shop_id": "-", "shop_name": "-",
            "error": (
                "当前 Cookie 账号无权限，未同步：" + "、".join(missing)
                + "。若这些店铺属于另一个赛狐账号，请重新粘贴该账号的 Cookie 后再同步。"
            ),
        })
    if permission_errors:
        # 同一原因会按日期重复很多次，这里只保留前 3 条，避免刷屏
        results.append({
            "shop_id": "-", "shop_name": "-",
            "error": "部分店铺抓取失败（前 3 条）：" + "；".join(permission_errors[:3]),
        })

    return results


# ---------------------------------------------------------------------------
# 多账号：遍历账号池逐个同步
# ---------------------------------------------------------------------------
def sync_all_accounts(
    kind: str = "search",
    target_date: Optional[str] = None,
    days: int = 1,
    account_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """遍历**所有赛狐账号**，逐个切换登录态执行同步。

    多账号的关键约束：一个 Cookie 只能看到自己权限内的店铺。
    所以必须「切账号 → 探测该账号店铺 → 拉取 → 再切下一个」，
    绝不能拿一份 Cookie 跑全量（那就是之前串号的根因）。

    Args:
        kind: ``search`` 搜索词双维度 / ``product`` 商品 / ``both`` 两者都跑。
        target_date: 搜索词指定单日。
        days: 搜索词回补天数。
        account_ids: 只同步指定账号（为空 = 全部账号）。

    Returns:
        ``{"accounts": [每个账号的摘要], "results": [明细行]}``
    """
    accounts = sf.list_accounts()
    if account_ids:
        wanted = {str(x) for x in account_ids}
        accounts = [a for a in accounts if a["id"] in wanted]

    if not accounts:
        return {
            "accounts": [],
            "results": [{
                "shop_id": "-", "shop_name": "-",
                "error": (
                    "还没有配置任何赛狐账号。请在「同步与 Cookie 设置」里"
                    "把每个赛狐账号的 Cookie 分别添加进来（可添加多个）。"
                ),
            }],
        }

    all_results: List[Dict[str, Any]] = []
    summaries: List[Dict[str, Any]] = []

    for acc in accounts:
        aid = acc["id"]
        label = acc.get("label") or aid

        if not acc.get("valid"):
            summaries.append({
                "account_id": aid, "account_label": label,
                "skipped": True, "error": "Cookie 不完整或已失效，已跳过",
            })
            continue

        tasks = ["product", "search"] if kind == "both" else [kind]
        acc_rows: List[Dict[str, Any]] = []
        acc_errors = 0

        for task in tasks:
            with sf.use_account(aid):
                try:
                    if task == "product":
                        res = sync_products_all_shops(aid)
                    else:
                        res = sync_search_terms_all_shops(target_date, days, aid)
                except Exception as exc:  # noqa: BLE001 - 单账号失败不拖垮其他账号
                    res = [{
                        "shop_id": "-", "shop_name": "-",
                        "error": f"{task} 同步失败：{exc}",
                    }]
            for r in res:
                r["account_id"] = aid
                r["account_label"] = label
                r["sync_kind"] = task
                if r.get("error"):
                    acc_errors += 1
            acc_rows.extend(res)

        sf.mark_account_sync(aid)
        all_results.extend(acc_rows)
        summaries.append({
            "account_id": aid,
            "account_label": label,
            "shops": len(acc.get("shops") or []),
            "rows": len(acc_rows),
            "errors": acc_errors,
        })

    return {"accounts": summaries, "results": all_results}


def sync_all_accounts_range(
    start_date: str,
    end_date: str,
    account_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """遍历所有账号，按**显式日期区间**补同步搜索词双维度。

    与 ``sync_all_accounts(kind="search")`` 的区别只在日期来源：
    那边「往前推 N 天」，这边「补指定区间」，用于填历史缺口。
    """
    accounts = sf.list_accounts()
    if account_ids:
        wanted = {str(x) for x in account_ids}
        accounts = [a for a in accounts if a["id"] in wanted]

    if not accounts:
        return {
            "accounts": [],
            "results": [{
                "shop_id": "-", "shop_name": "-",
                "error": "还没有配置任何赛狐账号，请先添加账号 Cookie。",
            }],
        }

    all_results: List[Dict[str, Any]] = []
    summaries: List[Dict[str, Any]] = []

    for acc in accounts:
        aid = acc["id"]
        label = acc.get("label") or aid

        if not acc.get("valid"):
            summaries.append({
                "account_id": aid, "account_label": label,
                "skipped": True, "error": "Cookie 不完整或已失效，已跳过",
            })
            continue

        with sf.use_account(aid):
            try:
                res = sync_search_terms_range(start_date, end_date, aid)
            except Exception as exc:  # noqa: BLE001 - 单账号失败不拖垮其他账号
                res = [{
                    "shop_id": "-", "shop_name": "-",
                    "error": f"区间补同步失败：{exc}",
                }]

        acc_errors = 0
        for r in res:
            r["account_id"] = aid
            r["account_label"] = label
            if r.get("error"):
                acc_errors += 1

        sf.mark_account_sync(aid)
        all_results.extend(res)
        summaries.append({
            "account_id": aid,
            "account_label": label,
            "shops": len(acc.get("shops") or []),
            "rows": len(res),
            "errors": acc_errors,
        })

    return {"accounts": summaries, "results": all_results}
