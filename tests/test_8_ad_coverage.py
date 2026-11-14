# -*- coding: utf-8 -*-
"""v2.7 验收测试：父 SKU 广告开启状态判定。

只测纯函数 ``build_coverage``（不碰网络/数据库），把判定规则锁死：
  1. 广告商品 SKU 属于父体 + 活动 enabled → 已开启
  2. 活动 paused / archived → 不算已开启（用户明确要求）
  3. 同一父体既有暂停又有开启 → 已开启
  4. 完全没有活动 → 未开启（none）
  5. 活动名里含父 SKU 时兜底判定（广告商品接口返回空的情况）
  6. SKU 大小写 / 空格不敏感
  7. 活动名里的 SKU 必须「整词命中」，hua22 不能命中 hua226
  8. 子体 MSKU 归属父体，父 SKU 自身也算
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.stdout.reconfigure(encoding="utf-8")

from core.ad_coverage import (  # noqa: E402
    build_coverage, campaign_state, campaign_type, ad_product_sku,
)

OK = FAIL = 0


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [PASS] {name} {detail}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


print("=" * 78)
print("v2.7 验收：父 SKU 广告开启状态判定")
print("=" * 78)

PARENTS = {
    "hua226": ["hua226-1", "hua226-2"],
    "hua216": ["hua216-1"],
    "hyy01": ["hyy01"],
    "hua22": [],
}

CAMPAIGNS = [
    {"campaignId": "c1", "campaignName": "hua226_犬小屋_自动_20261101", "state": "enabled"},
    {"campaignId": "c2", "campaignName": "hua216_收纳_手动_20261101", "state": "paused"},
    {"campaignId": "c3", "campaignName": "hyy01_垃圾桶_自动_20261101", "state": "archived"},
    {"campaignId": "c4", "campaignName": "hua216_收纳_自动_20261102", "state": "enabled"},
]

AD_PRODUCTS = [
    # hua226 的子体挂在 c1（enabled）
    {"campaignId": "c1", "sku": "HUA226-1", "asin": "B0AAAAAAAA"},
    # hua216 的子体同时挂在 c2（paused）和 c4（enabled）
    {"campaignId": "c2", "sku": "hua216-1", "asin": "B0BBBBBBBB"},
    {"campaignId": "c4", "sku": "hua216-1", "asin": "B0BBBBBBBB"},
    # hyy01 只挂在 c3（archived）
    {"campaignId": "c3", "sku": "hyy01", "asin": "B0CCCCCCCC"},
]

print("\n[1] 字段兼容：state / status / campaignStatus")
check("state 优先", campaign_state({"state": "ENABLED"}) == "enabled")
check("回退 status", campaign_state({"status": "paused"}) == "paused")
check("回退 campaignStatus", campaign_state({"campaignStatus": "Archived"}) == "archived")
check("全缺失返回空", campaign_state({}) == "")
check("SKU 取 sku", ad_product_sku({"sku": " a1 "}) == "A1")
check("SKU 回退 sellerSku", ad_product_sku({"sellerSku": "b2"}) == "B2")
check("SKU 回退 msku", ad_product_sku({"msku": "c3"}) == "C3")

print("\n[2] 主判定：广告商品 SKU + 活动状态")
cov = build_coverage(PARENTS, CAMPAIGNS, AD_PRODUCTS)
check("hua226 已开启", cov["hua226"]["enabled"] is True, str(cov["hua226"]["campaigns"]))
check("hua226 状态为 enabled", cov["hua226"]["state"] == "enabled")
check("子体 SKU 大写也能匹配", "hua226_犬小屋_自动_20261101" in cov["hua226"]["campaigns"])

print("\n[3] paused / archived 不算已开启")
check("hyy01 仅归档 → 未开启", cov["hyy01"]["enabled"] is False, str(cov["hyy01"]))
check("hyy01 状态为 paused（有活动但没开）", cov["hyy01"]["state"] == "paused")

print("\n[4] 同一父体既有暂停又有开启 → 已开启")
check("hua216 已开启", cov["hua216"]["enabled"] is True)
check("hua216 记录到暂停活动", len(cov["hua216"]["paused_campaigns"]) >= 1,
      str(cov["hua216"]["paused_campaigns"]))

print("\n[5] 完全没有广告 → none")
check("hua22 未开启", cov["hua22"]["enabled"] is False)
check("hua22 状态为 none", cov["hua22"]["state"] == "none")

print("\n[6] 兜底：活动名含父 SKU（广告商品接口返回空）")
cov2 = build_coverage(PARENTS, CAMPAIGNS, [])
check("无广告商品行时仍能识别 hua226", cov2["hua226"]["enabled"] is True)
check("无广告商品行时 hua216 已开启（c4 enabled）", cov2["hua216"]["enabled"] is True)

print("\n[7] 活动名整词匹配：hua22 不能命中 hua226")
cov3 = build_coverage(
    {"hua22": []},
    [{"campaignId": "c9", "campaignName": "hua226_犬小屋_自动", "state": "enabled"}],
    [],
)
check("hua22 不被误判为已开启", cov3["hua22"]["enabled"] is False, str(cov3["hua22"]))

print("\n[8] 汇总结构完整")
for pk in PARENTS:
    entry = cov[pk]
    check(f"{pk} 结构完整",
          set(entry) == {"enabled", "state", "campaigns", "paused_campaigns",
                         "auto_count", "manual_count", "other_count"},
          str(sorted(entry)))
    check(f"{pk} 活动名最多 3 个", len(entry["campaigns"]) <= 3)

print("\n[9] 活动类型判定（v2.11）")
check("targetingType=auto", campaign_type({"targetingType": "auto"}) == "auto")
check("targetingType 大小写不敏感", campaign_type({"targetingType": "MANUAL"}) == "manual")
check("字段缺失按活动名「手动」兜底",
      campaign_type({"campaignName": "hua226_犬小屋_手动_20261101"}) == "manual")
check("字段缺失按活动名「自动」兜底",
      campaign_type({"campaignName": "hua226_犬小屋_自动_20261101"}) == "auto")
check("英文名 Auto 兜底", campaign_type({"campaignName": "SP-Auto-hua226"}) == "auto")
check("判断不出返回空", campaign_type({"campaignName": "大杂烩-中客单-20260508"}) == "")

print("\n[10] 已开启的自动 / 手动广告条数")
# 同一活动下挂两条子体只能算 1 条；paused 不计入；类型不明单独归 other
CAMPAIGNS2 = [
    {"campaignId": "a1", "campaignName": "x1", "state": "enabled", "targetingType": "auto"},
    {"campaignId": "a2", "campaignName": "x2", "state": "enabled", "targetingType": "auto"},
    {"campaignId": "m1", "campaignName": "x3", "state": "enabled", "targetingType": "manual"},
    {"campaignId": "p1", "campaignName": "x4", "state": "paused", "targetingType": "manual"},
    {"campaignId": "u1", "campaignName": "x5", "state": "enabled"},
]
AD_PRODUCTS2 = [
    {"campaignId": "a1", "sku": "pp1"},
    {"campaignId": "a1", "sku": "pp1"},
    {"campaignId": "a2", "sku": "pp1"},
    {"campaignId": "m1", "sku": "pp1"},
    {"campaignId": "p1", "sku": "pp1"},
    {"campaignId": "u1", "sku": "pp1"},
]
cov4 = build_coverage({"pp1": ["pp1-1"]}, CAMPAIGNS2, AD_PRODUCTS2)
check("自动活动计 2 条", cov4["pp1"]["auto_count"] == 2, str(cov4["pp1"]))
check("手动活动计 1 条（暂停的不算）", cov4["pp1"]["manual_count"] == 1)
check("类型不明归 other", cov4["pp1"]["other_count"] == 1)
check("同活动多条子体不重复计数",
      cov4["pp1"]["auto_count"] + cov4["pp1"]["manual_count"] + cov4["pp1"]["other_count"] == 4)
check("没有任何广告的父体计数为 0",
      cov["hua22"]["auto_count"] == 0 and cov["hua22"]["manual_count"] == 0)
check("按活动名兜底也能分类", cov["hua226"]["auto_count"] == 1 and cov["hua226"]["manual_count"] == 0,
      f"auto={cov['hua226']['auto_count']} manual={cov['hua226']['manual_count']}")

print("\n" + "=" * 78)
print(f"结果：PASS {OK}  FAIL {FAIL}")
print("=" * 78)
sys.exit(1 if FAIL else 0)
