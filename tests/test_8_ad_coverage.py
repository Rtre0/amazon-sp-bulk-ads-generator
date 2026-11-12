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

from core.ad_coverage import build_coverage, campaign_state, ad_product_sku  # noqa: E402

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
          set(entry) == {"enabled", "state", "campaigns", "paused_campaigns"},
          str(sorted(entry)))
    check(f"{pk} 活动名最多 3 个", len(entry["campaigns"]) <= 3)

print("\n" + "=" * 78)
print(f"结果：PASS {OK}  FAIL {FAIL}")
print("=" * 78)
sys.exit(1 if FAIL else 0)
