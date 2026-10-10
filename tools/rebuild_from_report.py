# -*- coding: utf-8 -*-
"""从亚马逊回传的报错报告（report.xlsx）反向重建商品数据，并用 v9 生成器重出表格。

report.xlsx 的 ``Sponsored Products Campaigns`` 工作表会把「我们上传的原始行」
原样回显（后面再追加 初始工作表/记录#/错误类型/错误代码/错误消息 等列）。
因此它是最忠实的「这次上传到底提交了什么」的快照，用它反推商品列表可以保证
重出的表格与失败那次**商品集合完全一致**。

用法：
    python tools/rebuild_from_report.py <report.xlsx> <输出目录> [--placement-top N] [--placement-product N]

产出：
    三头牛-JP_SP_Bulk_v9_全量.xlsx        —— 全部活动
    三头牛-JP_SP_Bulk_v9_测试_仅1个活动.xlsx —— 子体最多的那个活动（便于先小文件试传）
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List

import openpyxl

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.stdout.reconfigure(encoding="utf-8")

from core.bulk_generator import generate_bulk_excel  # noqa: E402

SHEET = "Sponsored Products Campaigns"


def parse_products(report_path: str) -> List[Dict[str, Any]]:
    """把回显的原始行还原成 generate_bulk_excel 需要的 products 结构。"""
    wb = openpyxl.load_workbook(report_path, data_only=True)
    ws = wb[SHEET]
    header = [c.value for c in ws[1]]
    idx = {h: i for i, h in enumerate(header)}
    for need in ("实体层级", "广告活动编号", "广告活动名称", "每日预算", "SKU", "ASIN"):
        if need not in idx:
            raise SystemExit(f"报告缺少列：{need}")

    order: List[str] = []
    products: Dict[str, Dict[str, Any]] = {}

    for row in ws.iter_rows(min_row=2, values_only=True):
        entity = row[idx["实体层级"]]
        cid = row[idx["广告活动编号"]]
        if not entity or not cid:
            continue
        cid = str(cid)

        if entity == "Campaign":
            cname = str(row[idx["广告活动名称"]] or "")
            # 活动名格式：{parent_sku}_{中文品名}_自动_{YYYYMMDD}
            tail = cname[len(cid) + 1:] if cname.startswith(cid + "_") else cname
            prod_name = tail.rsplit("_自动_", 1)[0] or "商品"
            budget = row[idx["每日预算"]]
            try:
                budget = float(budget)
            except (TypeError, ValueError):
                budget = 100.0
            if cid not in products:
                order.append(cid)
            products[cid] = {
                "parent_sku": cid,
                "product_name": prod_name,
                "assigned_budget": budget,
                "children": [],
            }
        elif entity == "Product Ad":
            if cid not in products:
                # 防御：没有 Campaign 行时也能兜底
                order.append(cid)
                products[cid] = {
                    "parent_sku": cid,
                    "product_name": "商品",
                    "assigned_budget": 100.0,
                    "children": [],
                }
            products[cid]["children"].append(
                {
                    "msku": str(row[idx["SKU"]] or ""),
                    "asin": str(row[idx["ASIN"]] or ""),
                }
            )

    return [products[c] for c in order if products[c]["children"]]


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    report_path, out_dir = sys.argv[1], sys.argv[2]
    top = prod = 0
    if "--placement-top" in sys.argv:
        top = float(sys.argv[sys.argv.index("--placement-top") + 1])
    if "--placement-product" in sys.argv:
        prod = float(sys.argv[sys.argv.index("--placement-product") + 1])

    products = parse_products(report_path)
    n_child = sum(len(p["children"]) for p in products)
    print(f"从报告还原：{len(products)} 个活动 / {n_child} 个子体")

    settings = {
        "bidding_strategy": "Fixed bid",
        "default_bid": 25,
        "bid_close": 25,
        "bid_loose": 20,
        "bid_substitutes": 25,
        "bid_complements": 20,
        "include_targeting": True,
        "include_placement": True,
        "placement_top_pct": top,
        "placement_product_pct": prod,
    }

    os.makedirs(out_dir, exist_ok=True)
    full = os.path.join(out_dir, "三头牛-JP_SP_Bulk_v9_全量.xlsx")
    with open(full, "wb") as fh:
        fh.write(generate_bulk_excel(products, settings).getvalue())
    print(f"已写出 {full}  ({os.path.getsize(full)} 字节)")

    biggest = max(products, key=lambda p: len(p["children"]))
    smallest = min(products, key=lambda p: len(p["children"]))
    one = os.path.join(out_dir, "三头牛-JP_SP_Bulk_v9_测试_仅1个活动.xlsx")
    with open(one, "wb") as fh:
        fh.write(generate_bulk_excel([smallest], settings).getvalue())
    print(
        f"已写出 {one}  ({os.path.getsize(one)} 字节)  "
        f"活动={smallest['parent_sku']} 子体={len(smallest['children'])}"
        f"（最小活动；全量中最大活动 {biggest['parent_sku']} 有 {len(biggest['children'])} 个子体）"
    )


if __name__ == "__main__":
    main()
