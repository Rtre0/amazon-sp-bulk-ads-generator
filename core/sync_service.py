"""数据同步服务。

两大核心任务：
1. **商品每周增量同步**：拉取赛狐在线商品 → 本地名词词库清洗 → 增量入库（跳过已存在）
2. **搜索词每日凌晨同步**：拉取昨日活动列表 → 广告产品映射 → 搜索词数据 → 二次过滤入库

设计要点
--------
- 商品同步以 ``(shop_id, msku, asin)`` 唯一键去重，已存在的旧商品直接丢弃，
  不覆盖用户已微调过的中文品名；仅入库新上架商品。
- 搜索词同步以 ``(report_date, shop_id, search_term, campaign_id, match_type)`` 去重。
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


def discover_and_register_shops() -> List[Dict[str, Any]]:
    """自动发现账号下全部店铺并写入 shops 表。

    赛狐没有独立店铺列表接口，改为从广告活动接口的 ``shopId`` / ``shopName``
    反推（详见 ``sellfox_client.discover_shops``）。
    """
    discovered = sf.discover_shops()
    for d in discovered:
        db.upsert_shop(d["shop_id"], d["shop_name"])
    return discovered


def sync_products_for_shop(
    shop_id: str,
    shop_name: str = "",
    marketplace: str = "A1VC38T7YXB528",
) -> Dict[str, Any]:
    """拉取**单个店铺**的全量在线商品并增量入库。"""
    db.upsert_shop(shop_id, shop_name, marketplace)

    all_rows = sf.fetch_all_products(shop_id)
    stat = _insert_product_rows(all_rows, shop_id, shop_name)
    db.update_shop_sync_time(shop_id, "product")

    return {
        "shop_id": shop_id,
        "shop_name": shop_name,
        "total_fetched": len(all_rows),
        **stat,
    }


def sync_products_all_shops() -> List[Dict[str, Any]]:
    """同步全账号商品（逐店铺拉取）。

    ⚠️ 为什么逐店铺拉而不是「一次拉全账号再分组」：
    商品接口的排序是固定的（按店铺聚集），单店商品可达数千条。
    一次拉全账号会在 ``max_pages`` 处截断，排在后面的店铺**完全抓不到**
    （实测 5 店铺时只抓到前 2 个）。逐店铺拉可保证每家店都翻到底。

    shops 表为空时自动执行店铺发现。
    """
    shops = db.list_shops()

    # 没有任何店铺 → 自动发现
    if not shops:
        try:
            discover_and_register_shops()
        except Exception as exc:
            return [{
                "shop_id": "-",
                "shop_name": "-",
                "error": f"自动发现店铺失败（请检查 Cookie 是否过期）：{exc}",
            }]
        shops = db.list_shops()
        if not shops:
            return [{
                "shop_id": "-",
                "shop_name": "-",
                "error": "未能发现任何店铺：账号下没有广告活动或商品，请先在赛狐后台确认店铺已授权",
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

    # ⚠️ 性能关键（v2.1）：**不传 campaignIdList**（传空数组）直接拉全店铺搜索词。
    #    实测该接口在 campaignIdList=[] 时返回该店铺全部搜索词（含出单行），
    #    耗时约 6.6 秒；而「先拉 2169 个活动（11 页，16s）→ 再分批拉搜索词（6 批）」
    #    需要 43 秒。省掉活动列表这一步，单店单日从 43s 降到约 9s。
    search_rows = sf.fetch_search_terms(
        shop_id=shop_id,
        campaign_ids=[],          # 空 = 全店铺
        start_date=target_date,
        end_date=target_date,
        query_type="searchKeyword",
    )

    if not search_rows:
        return {
            "shop_id": shop_id,
            "shop_name": shop_name,
            "report_date": target_date,
            "campaigns": 0,
            "active_campaigns": 0,
            "search_terms_fetched": 0,
            "search_terms_inserted": 0,
            "orders_found": 0,
            "message": "该日无搜索词数据（可能无曝光/花费）",
        }

    # 从搜索词行反推涉及的活动 ID（只对这些活动查广告产品做 ASIN/SKU 映射）
    campaign_ids = sorted({
        str(r.get("campaignId")) for r in search_rows if r.get("campaignId")
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
        "orders_found": sum(1 for r in db_rows if int(r.get("ad_orders") or 0) > 0),
    }


def sync_search_terms_all_shops(
    target_date: Optional[str] = None,
    days: int = 1,
) -> List[Dict[str, Any]]:
    """同步全账号店铺的搜索词数据。

    Args:
        target_date: 指定单日（YYYY-MM-DD）。给了就只同步这一天。
        days: 未指定 target_date 时，回补最近 N 天（1 = 仅昨天）。

    shops 表为空时自动先发现店铺（与商品同步保持一致）。

    为什么要「回补 N 天」：单日出单很少（实测某店 30 天仅 57 单），
    只看昨天常常一条出单词都没有。回补 7 天才能凑出足够的拓词样本。
    """
    shops = db.list_shops()

    if not shops:
        try:
            discover_and_register_shops()
        except Exception as exc:
            return [{
                "shop_id": "-",
                "shop_name": "-",
                "error": f"自动发现店铺失败（请检查 Cookie 是否过期）：{exc}",
            }]
        shops = db.list_shops()
        if not shops:
            return [{
                "shop_id": "-",
                "shop_name": "-",
                "error": "未能发现任何店铺：账号下没有广告活动或商品",
            }]

    # 计算要同步的日期列表
    if target_date:
        dates = [target_date]
    else:
        days = max(1, min(int(days or 1), 30))  # 夹取 1~30 天
        today = datetime.date.today()
        dates = [
            (today - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(1, days + 1)
        ]

    results: List[Dict[str, Any]] = []

    for date_str in dates:
        for shop in shops:
            try:
                result = sync_search_terms_for_shop(
                    shop_id=shop["shop_id"],
                    shop_name=shop["shop_name"],
                    target_date=date_str,
                )
                results.append(result)
            except Exception as exc:
                results.append({
                    "shop_id": shop["shop_id"],
                    "shop_name": shop["shop_name"],
                    "report_date": date_str,
                    "error": str(exc),
                })

    return results
