# -*- coding: utf-8 -*-
"""v9 端到端验收：解析 -> 预算 -> 生成 Bulk，并做表头对齐、结构、预算模式、字段合规与零网络断言。"""
import io
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.stdout.reconfigure(encoding="utf-8")

import openpyxl  # noqa: E402

from core.budget_engine import assign_budgets, build_shop_payload  # noqa: E402
from core.bulk_generator import (  # noqa: E402
    AUTO_TARGETING,
    BULK_COLUMNS,
    HELPER_COLUMNS,
    OFFICIAL_COLUMNS,
    SHEET_NAME,
    TARGETING_EXPR_COLUMN,
    VALID_ENTITIES,
    generate_bulk_excel,
)
from core.parser import parse_sellfox_excel_multi_shop  # noqa: E402

SAMPLE = os.path.join(BASE, "samples", "多店铺测试样本", "赛狐导出_多店铺日文品名.xlsx")
TEMPLATE = os.path.join(BASE, "samples", "AdvertisingBulksheetTemplate-seller.xlsx")
OK = 0
FAIL = 0


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [PASS] {name} {detail}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


print("=" * 78)
print("端到端验收：多店铺样本 -> Bulk Excel（v9 表头对齐官方模板）")
print("=" * 78)

raw = open(SAMPLE, "rb").read()
parsed = parse_sellfox_excel_multi_shop(raw, enable_translation=True)
shop_payloads = build_shop_payload(parsed, base_budget=100.0)

print("\n[0] 表头与亚马逊官方模板逐字对齐（v9 事故回归）")
check("官方列数 = 31", len(OFFICIAL_COLUMNS) == 31, f"实际 {len(OFFICIAL_COLUMNS)}")
check("总列数 = 31 官方 + ASIN 辅助", len(BULK_COLUMNS) == 32, f"实际 {len(BULK_COLUMNS)}")
check("辅助列只有 ASIN", HELPER_COLUMNS == ["ASIN"], str(HELPER_COLUMNS))
if os.path.exists(TEMPLATE):
    twb = openpyxl.load_workbook(TEMPLATE)
    tws = twb["商品推广活动"]
    tpl_header = [c.value for c in tws[1]][:31]
    check("官方模板 31 列表头与 OFFICIAL_COLUMNS 完全一致", tpl_header == OFFICIAL_COLUMNS,
          "" if tpl_header == OFFICIAL_COLUMNS else f"差异：{[(i, a, b) for i, (a, b) in enumerate(zip(tpl_header, OFFICIAL_COLUMNS)) if a != b]}")
    check("官方模板中第 27 列 = 拓展商品投放编号",
          tpl_header[26] == TARGETING_EXPR_COLUMN == "拓展商品投放编号", repr(tpl_header[26]))
else:
    check("官方模板存在", False, TEMPLATE)
check("已废弃旧表头不再出现",
      not ({"商品投放表达式", "商品投放编号", "关键词"} & set(BULK_COLUMNS)),
      str(sorted({"商品投放表达式", "商品投放编号", "关键词"} & set(BULK_COLUMNS))))
check("官方表头含 商品投放 ID / 关键词文本",
      "商品投放 ID" in OFFICIAL_COLUMNS and "关键词文本" in OFFICIAL_COLUMNS)

print("\n[1] 解析与聚合")
n_shop = len(shop_payloads)
n_parent = sum(len(s["products"]) for s in shop_payloads.values())
check("店铺数 = 4", n_shop == 4, f"实际 {n_shop}")
check("父商品总数 = 44", n_parent == 44, f"实际 {n_parent}")
sample_shop = next(iter(shop_payloads))
p0 = shop_payloads[sample_shop]["products"][0]
check("含 parent_sku", bool(p0.get("parent_sku")), p0.get("parent_sku"))
check("含中文品名", bool(p0.get("product_name")), p0.get("product_name"))
check("含均价", float(p0.get("avg_price", 0)) > 0, f"均价 {p0.get('avg_price')}")
check("含建议预算", float(p0.get("suggested_budget", 0)) > 0, f"预算 {p0.get('suggested_budget')}")

print("\n[2] 预算模式")
prod = shop_payloads[sample_shop]["products"]
eq = assign_budgets(prod, mode="equal", value=100.0, min_budget=100.0)
check("等权模式：全部 = 100", all(abs(float(x["assigned_budget"]) - 100.0) < 1e-6 for x in eq),
      f"示例 {[x['assigned_budget'] for x in eq[:4]]}")
wt = assign_budgets(prod, mode="weighted", value=2000.0, min_budget=100.0)
tot = sum(float(x["assigned_budget"]) for x in wt)
check("加权模式：总池 ≈ 2000（下限截断前）", tot >= 2000.0, f"合计 {tot:.0f}")
check("加权模式：预算随价格单调（高价 >= 低价）",
      all(float(a["assigned_budget"]) >= float(b["assigned_budget"])
          for a, b in zip(sorted(wt, key=lambda x: -float(x["avg_price"])),
                          sorted(wt, key=lambda x: -float(x["avg_price"]))[1:])),
      f"前4 {[x['assigned_budget'] for x in sorted(wt, key=lambda x: -float(x['avg_price']))[:4]]}")

print("\n[3] Bulk 生成（含 Targeting + 非零广告位溢价）")
all_products = [p for s in shop_payloads.values() for p in s["products"]]
n_child = sum(int(p.get("child_count") or len(p.get("children") or [])) for p in all_products)
print(f"     父体 {len(all_products)} 个，子体 {n_child} 个")
buf = generate_bulk_excel(all_products, {
    "include_targeting": True, "include_placement": True,
    "placement_top_pct": 30, "placement_product_pct": 10,
})
wb = openpyxl.load_workbook(buf)
check("单 Sheet", wb.sheetnames == [SHEET_NAME], str(wb.sheetnames))
ws = wb[SHEET_NAME]
check("列数 = 32", ws.max_column == len(BULK_COLUMNS) == 32, f"实际 {ws.max_column}")
header = [c.value for c in ws[1]]
check("表头与 BULK_COLUMNS 一致", header == BULK_COLUMNS, str(header[:3]) + " ...")
check("表头前 31 列 = 官方模板", header[:31] == OFFICIAL_COLUMNS, str(header[25:28]))

entities = {}
for row in ws.iter_rows(min_row=2, values_only=True):
    lvl = row[BULK_COLUMNS.index("实体层级")]
    entities[lvl] = entities.get(lvl, 0) + 1
print(f"     实体分布: {entities}")
check("Campaign 行数 = 父体数", entities.get("Campaign") == len(all_products),
      f"实际 {entities.get('Campaign')}")
check("Ad Group 行数 = 父体数", entities.get("Ad Group") == len(all_products),
      f"实际 {entities.get('Ad Group')}")
check("Product Ad 行数 = 子体数（每个子 SKU 一行）", entities.get("Product Ad") == n_child,
      f"实际 {entities.get('Product Ad')} / 子体 {n_child}")
check("Product Targeting 行数 = 父体×4", entities.get("Product Targeting") == len(all_products) * 4,
      f"实际 {entities.get('Product Targeting')}")
check("不存在非法的 Targeting 实体（v8 事故回归）", entities.get("Targeting", 0) == 0,
      f"实际 {entities.get('Targeting')}")
check("实体层级全部合法", set(entities) <= VALID_ENTITIES, str(sorted(set(entities) - VALID_ENTITIES)))
check("Bidding Adjustment = 父体×2（顶部+商品页）",
      entities.get("Bidding Adjustment") == len(all_products) * 2,
      f"实际 {entities.get('Bidding Adjustment')}")
total_rows = ws.max_row - 1
expect = len(all_products) * 2 + n_child + len(all_products) * 4 + entities.get("Bidding Adjustment", 0)
check("总行数自洽", total_rows == expect, f"实际 {total_rows} / 期望 {expect}")

# ---- v9 字段值合规性（这四项任一写错都会导致整表被拒）----
idx = {c: i for i, c in enumerate(BULK_COLUMNS)}
pt_rows = [r for r in ws.iter_rows(min_row=2, values_only=True)
           if r[idx["实体层级"]] == "Product Targeting"]
check(f"自动投放表达式写入「{TARGETING_EXPR_COLUMN}」且为裸受控值",
      {r[idx[TARGETING_EXPR_COLUMN]] for r in pt_rows} == set(AUTO_TARGETING),
      str(sorted({r[idx[TARGETING_EXPR_COLUMN]] for r in pt_rows})))
check(f"「{TARGETING_EXPR_COLUMN}」列无空值（v9 事故回归）",
      all(str(r[idx[TARGETING_EXPR_COLUMN]] or "").strip() for r in pt_rows))
check("表达式不含 targeting_expression= 包装",
      not any("targeting_expression" in str(r[idx[TARGETING_EXPR_COLUMN]]) for r in pt_rows))
check("投放行「关键词文本」「匹配类型」留空",
      all(not r[idx["关键词文本"]] and not r[idx["匹配类型"]] for r in pt_rows))
camp_tt = {r[idx["投放类型"]] for r in ws.iter_rows(min_row=2, values_only=True)
           if r[idx["实体层级"]] == "Campaign"}
check("Campaign 投放类型 = Auto（首字母大写）", camp_tt == {"Auto"}, str(camp_tt))
plc = {r[idx["广告位"]] for r in ws.iter_rows(min_row=2, values_only=True)
       if r[idx["实体层级"]] == "Bidding Adjustment"}
check("广告位为受控值", plc == {"Placement Top", "Placement Product Page"}, str(sorted(plc)))
pcts = {r[idx["百分比"]] for r in ws.iter_rows(min_row=2, values_only=True)
        if r[idx["实体层级"]] == "Bidding Adjustment"}
check("百分比为数字且在 0-900", pcts == {30, 10}, str(sorted(pcts, key=str)))

print("\n[3b] 广告位溢价为 0 时不生成空操作行")
buf_zero = generate_bulk_excel(all_products, {
    "include_targeting": True, "include_placement": True,
    "placement_top_pct": 0, "placement_product_pct": 0,
})
ws_zero = openpyxl.load_workbook(buf_zero)[SHEET_NAME]
ent_zero = {}
for row in ws_zero.iter_rows(min_row=2, values_only=True):
    lvl = row[BULK_COLUMNS.index("实体层级")]
    ent_zero[lvl] = ent_zero.get(lvl, 0) + 1
check("0 溢价 -> 无 Bidding Adjustment 行", ent_zero.get("Bidding Adjustment", 0) == 0,
      f"实际 {ent_zero.get('Bidding Adjustment', 0)}")
check("0 溢价 -> 其余行结构不变",
      ent_zero.get("Campaign") == len(all_products)
      and ent_zero.get("Product Targeting") == len(all_products) * 4,
      str(ent_zero))

print("\n[4] Bulk 生成（不含 Targeting / Placement，对照精简模板）")
buf2 = generate_bulk_excel(all_products, {"include_targeting": False, "include_placement": False})
wb2 = openpyxl.load_workbook(buf2)
ws2 = wb2[SHEET_NAME]
ent2 = {}
for row in ws2.iter_rows(min_row=2, values_only=True):
    lvl = row[BULK_COLUMNS.index("实体层级")]
    ent2[lvl] = ent2.get(lvl, 0) + 1
print(f"     实体分布: {ent2}")
check("无 Product Targeting 行", ent2.get("Product Targeting", 0) == 0)
check("无 Bidding Adjustment 行", ent2.get("Bidding Adjustment", 0) == 0)
check("行数 = 父体×2 + 子体", ws2.max_row - 1 == len(all_products) * 2 + n_child,
      f"实际 {ws2.max_row - 1} / 期望 {len(all_products) * 2 + n_child}")

print("\n[5] 命名规范")
camp_names = set()
for row in ws.iter_rows(min_row=2, values_only=True):
    if row[BULK_COLUMNS.index("实体层级")] == "Campaign":
        camp_names.add(row[BULK_COLUMNS.index("广告活动名称")])
print(f"     活动名示例: {sorted(camp_names)[:3]}")
check("活动名含中文品名与日期", all("自动_" in n for n in camp_names), f"共 {len(camp_names)} 个")

print("\n[6] 全程零网络：屏蔽 socket 后重跑完整解析链路")
import socket  # noqa: E402


class _BlockedSocket(socket.socket):
    def connect(self, *a, **k):
        raise AssertionError("检测到网络调用！v6 应为纯离线")

    def connect_ex(self, *a, **k):
        raise AssertionError("检测到网络调用！v6 应为纯离线")


_orig = socket.socket
socket.socket = _BlockedSocket
try:
    t0 = time.time()
    parsed_off = parse_sellfox_excel_multi_shop(raw)
    payload_off = build_shop_payload(parsed_off, base_budget=100.0)
    buf_off = generate_bulk_excel(
        [p for s in payload_off.values() for p in s["products"]], {"include_targeting": True}
    )
    dt_off = time.time() - t0
    check("断网下解析成功", len(payload_off) == 4, f"{len(payload_off)} 店铺, {dt_off:.3f}s")
    check("断网下导出成功", len(buf_off.getvalue()) > 10000, f"{len(buf_off.getvalue())} 字节")
finally:
    socket.socket = _orig

print("\n[7] 上传前自检 _validate_output（负向 / 正向用例）")
import pandas as pd  # noqa: E402

from core.bulk_generator import _validate_output  # noqa: E402


def _frame(entity, overrides=None):
    """构造一行「官方 31 列齐全」的合规数据（实体=entity），再叠加 overrides。"""
    row = {c: None for c in BULK_COLUMNS}
    row["产品"] = "Sponsored Products"
    row["实体层级"] = entity
    row["操作"] = "Create"
    if entity == "Product Targeting":
        row[TARGETING_EXPR_COLUMN] = "close-match"
    elif entity == "Bidding Adjustment":
        row["广告位"] = "Placement Top"
        row["百分比"] = 10
    elif entity == "Campaign":
        row["投放类型"] = "Auto"
    if overrides:
        row.update(overrides)
    return pd.DataFrame([row])[BULK_COLUMNS]


def _reject(name, entity, overrides):
    try:
        _validate_output(_frame(entity, overrides))
        check(name, False, "自检未拦截")
    except ValueError as exc:
        check(name, True, f"已拦截：{str(exc).splitlines()[-1][:56]}")


def _accept(name, entity, overrides=None):
    try:
        _validate_output(_frame(entity, overrides))
        check(name, True)
    except ValueError as exc:
        check(name, False, str(exc))


_reject("拦截非法实体 Targeting", "Product Targeting", {"实体层级": "Targeting"})
_reject("拦截带包装的表达式", "Product Targeting",
        {TARGETING_EXPR_COLUMN: 'targeting_expression = "close-match"'})
_reject("拦截投放行表达式为空（v9 事故回归）", "Product Targeting", {TARGETING_EXPR_COLUMN: None})
_reject("拦截投放行表达式为空白串", "Product Targeting", {TARGETING_EXPR_COLUMN: "   "})
_reject("拦截投放行误填关键词", "Product Targeting", {"关键词文本": "water tank"})
_reject("拦截非法广告位", "Bidding Adjustment", {"广告位": "placement top"})
_reject("拦截超范围百分比", "Bidding Adjustment", {"百分比": 1200})
_reject("拦截小写投放类型", "Campaign", {"投放类型": "auto"})

# 缺失整列必须被拦截
_missing = pd.DataFrame([{c: None for c in BULK_COLUMNS if c != TARGETING_EXPR_COLUMN}])
try:
    _validate_output(_missing)
    check("拦截整列缺失（拓展商品投放编号）", False, "自检未拦截")
except ValueError as exc:
    check("拦截整列缺失（拓展商品投放编号）", True,
          f"已拦截：{str(exc).splitlines()[-1][:56]}")

# 旧表头残留必须被拦截
_legacy = pd.DataFrame([{**{c: None for c in BULK_COLUMNS}, "商品投放表达式": "close-match"}])
try:
    _validate_output(_legacy)
    check("拦截旧表头 商品投放表达式 残留", False, "自检未拦截")
except ValueError as exc:
    check("拦截旧表头 商品投放表达式 残留", True,
          f"已拦截：{str(exc).splitlines()[-1][:56]}")

for _e in ["Campaign", "Ad Group", "Product Ad", "Bidding Adjustment", "Product Targeting"]:
    _accept(f"合规数据通过自检（{_e}）", _e)
for _expr in AUTO_TARGETING:
    _accept(f"四种自动投放表达式均通过（{_expr}）", "Product Targeting", {TARGETING_EXPR_COLUMN: _expr})

print("\n" + "=" * 78)
print(f"结果：PASS {OK}  FAIL {FAIL}")
print("=" * 78)
sys.exit(1 if FAIL else 0)
