# -*- coding: utf-8 -*-
"""判断「某个父 SKU 现在有没有已开启的广告」。

背景
----
自动广告工作台是按「父 SKU」铺商品的，但铺之前得知道这个父体**是不是已经开过
广告了**。以前只能靠人工去赛狐后台逐个翻，容易重复建活动、白烧预算。

数据来源
--------
赛狐里「父 SKU → 广告」的链路是：
``campaign/getAllCampaignData``（活动 + 状态 state）→
``multiple/adProduct/getAdProductList``（活动下挂的广告商品 sku/asin）

所以判定很简单：**广告商品 SKU 属于这个父体，且它所在活动的 state = enabled**。
paused / archived 一律不算（用户明确要求：只认已开启）。

兜底
----
万一某个活动查不到广告商品行（接口偶发空），再用活动名里是否包含父 SKU 兜一次，
避免「明明开着却显示未开启」。
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, Iterable, List, Optional

# 状态字段在不同接口/版本里叫法不一，按优先级依次取
_STATE_KEYS = ("state", "status", "campaignStatus")
_SKU_KEYS = ("sku", "sellerSku", "msku")
_ENABLED = "enabled"

#: 单次批量查询广告商品的活动数（太大容易被接口截断）
CAMPAIGN_BATCH_SIZE = 80
#: 每批广告商品最多翻多少页（500/页，够覆盖单批 80 个活动的全部商品）
AD_PRODUCT_MAX_PAGES = 10


def _norm_sku(value: Any) -> str:
    """SKU 归一化：去空格、转大写（亚马逊 SKU 大小写不敏感）。"""
    return str(value or "").strip().upper()


def campaign_state(campaign: Dict[str, Any]) -> str:
    """读取活动状态，统一成小写字符串（enabled / paused / archived / ''）。"""
    for key in _STATE_KEYS:
        val = campaign.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip().lower()
    return ""


def ad_product_sku(row: Dict[str, Any]) -> str:
    """读取广告商品行里的 SKU。"""
    for key in _SKU_KEYS:
        val = row.get(key)
        if val:
            return _norm_sku(val)
    return ""


def _campaign_id(campaign: Dict[str, Any]) -> str:
    for key in ("campaignId", "campaign_id", "id"):
        val = campaign.get(key)
        if val not in (None, ""):
            return str(val)
    return ""


def _campaign_name(campaign: Dict[str, Any]) -> str:
    for key in ("campaignName", "campaign_name", "name"):
        val = campaign.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def build_coverage(
    parents: Dict[str, Iterable[str]],
    campaigns: List[Dict[str, Any]],
    ad_products: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """纯函数：算出每个父 SKU 的广告开启状态（便于单测，不碰网络/数据库）。

    Args:
        parents: ``{父SKU: [子体 MSKU, ...]}``；父 SKU 自身也会参与匹配。
        campaigns: 活动列表，需含 ``campaignId`` / ``state``（可选 ``campaignName``）。
        ad_products: 广告商品列表，需含 ``campaignId`` / ``sku``。

    Returns:
        ``{父SKU: {"enabled": bool, "state": "enabled"|"paused"|"none",
        "campaigns": [已开启的活动名], "paused_campaigns": [暂停的活动名]}}``
    """
    # SKU → 父 SKU（一个 MSKU 理论上只属于一个父体，用 set 兜住脏数据）
    sku_to_parents: Dict[str, set] = {}
    for parent_sku, mskus in parents.items():
        pk = str(parent_sku or "").strip()
        if not pk:
            continue
        for sku in [pk, *(mskus or [])]:
            norm = _norm_sku(sku)
            if norm:
                sku_to_parents.setdefault(norm, set()).add(pk)

    state_by_cid: Dict[str, str] = {}
    name_by_cid: Dict[str, str] = {}
    for camp in campaigns or []:
        cid = _campaign_id(camp)
        if not cid:
            continue
        state_by_cid[cid] = campaign_state(camp)
        name_by_cid[cid] = _campaign_name(camp)

    result: Dict[str, Dict[str, Any]] = {
        str(pk): {"enabled": False, "state": "none",
                  "campaigns": [], "paused_campaigns": []}
        for pk in parents.keys()
    }

    def _mark(parent_sku: str, cid: str) -> None:
        entry = result.get(parent_sku)
        if entry is None:
            return
        state = state_by_cid.get(cid, "")
        label = name_by_cid.get(cid) or cid
        if state == _ENABLED:
            entry["enabled"] = True
            if label not in entry["campaigns"]:
                entry["campaigns"].append(label)
        elif state:
            if label not in entry["paused_campaigns"]:
                entry["paused_campaigns"].append(label)

    # 1) 主判据：广告商品 SKU → 父体 + 活动状态
    for row in ad_products or []:
        cid = str(row.get("campaignId") or row.get("campaign_id") or "")
        if not cid:
            continue
        for parent_sku in sku_to_parents.get(ad_product_sku(row), ()):
            _mark(parent_sku, cid)

    # 2) 兜底：活动名里含父 SKU（广告商品接口偶尔返回空）
    #    ⚠️ 不能写成「活动 × 父体」双循环：2000 活动 × 500 父体 = 100 万次正则，
    #    实测要跑几十秒。这里把全部父 SKU 编成**一个**交替正则，每条活动名扫一次。
    parent_keys = [str(pk) for pk in parents.keys() if len(str(pk)) >= 2]
    if parent_keys and name_by_cid:
        ordered = sorted(parent_keys, key=len, reverse=True)
        pattern = re.compile(
            r"(?<![0-9A-Za-z])(" + "|".join(re.escape(s) for s in ordered) + r")(?![0-9A-Za-z])",
            re.IGNORECASE,
        )
        upper_to_parent = {s.upper(): s for s in parent_keys}
        for cid, name in name_by_cid.items():
            if not name:
                continue
            for match in pattern.finditer(name):
                parent_sku = upper_to_parent.get(match.group(1).upper())
                if parent_sku:
                    _mark(parent_sku, cid)

    # 3) 汇总状态：只要有一个 enabled 就算开启；否则有活动记为 paused
    for entry in result.values():
        if entry["enabled"]:
            entry["state"] = _ENABLED
        elif entry["paused_campaigns"]:
            entry["state"] = "paused"
        else:
            entry["state"] = "none"
        # 只留前 3 个活动名，前端 tooltip 用
        entry["campaigns"] = entry["campaigns"][:3]
        entry["paused_campaigns"] = entry["paused_campaigns"][:3]
    return result


def fetch_coverage(
    shop_id: str,
    parents: Dict[str, Iterable[str]],
    days: int = 60,
    client: Optional[Any] = None,
) -> Dict[str, Any]:
    """从赛狐拉活动 + 广告商品，算出该店铺全部父体的广告开启状态。

    Args:
        shop_id: 店铺 ID
        parents: ``{父SKU: [子体 MSKU]}``
        days: 回看天数（活动列表按区间查，给宽一点才不会漏掉「近 60 天没花钱但还开着」的活动）
        client: 便于测试注入（默认用 ``core.sellfox_client``）
    """
    import datetime

    sf = client
    if sf is None:
        from core import sellfox_client as sf  # noqa: WPS433 - 延迟导入避免循环

    end = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    start = (datetime.date.today() - datetime.timedelta(days=max(1, days))).isoformat()

    campaigns = sf.fetch_all_campaigns(
        shop_id, start, end, status_list=["enabled", "paused", "archived"]
    )
    # 只查「已开启」活动下的广告商品：用户只关心这个父体有没有开着的广告。
    # 一个成熟店铺 60 天能有 2000+ 活动，全查一遍要 1~2 分钟；只查 enabled
    # 通常只剩三分之一。暂停/归档的父体用活动名兜底判断即可。
    enabled_ids = [
        _campaign_id(c) for c in campaigns if campaign_state(c) == _ENABLED
    ]
    enabled_ids = [cid for cid in enabled_ids if cid]

    ad_products: List[Dict[str, Any]] = []
    for i in range(0, len(enabled_ids), CAMPAIGN_BATCH_SIZE):
        batch = enabled_ids[i : i + CAMPAIGN_BATCH_SIZE]
        try:
            ad_products.extend(
                sf.fetch_ad_products(
                    shop_id=shop_id,
                    campaign_ids=batch,
                    start_date=start,
                    end_date=end,
                    max_pages=AD_PRODUCT_MAX_PAGES,
                )
            )
        except Exception:  # noqa: BLE001 - 单批失败不影响整体判定
            pass
        time.sleep(0.1)

    coverage = build_coverage(parents, campaigns, ad_products)
    enabled = sum(1 for v in coverage.values() if v["enabled"])
    paused = sum(1 for v in coverage.values() if v["state"] == "paused")
    return {
        "shop_id": shop_id,
        "range": {"start": start, "end": end},
        "campaigns": len(campaigns),
        "ad_products": len(ad_products),
        "coverage": coverage,
        "summary": {
            "total": len(coverage),
            "enabled": enabled,
            "paused": paused,
            "none": len(coverage) - enabled - paused,
        },
    }
