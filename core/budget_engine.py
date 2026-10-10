"""预算计算引擎。

职责：
1. 按店铺分别计算「全店有效商品价格中位数」P̄（多店铺隔离，互不干扰）。
2. 计算每个父体的代表客单价 Pi 与中位数比率 ratio。
3. 提供两种预算分配策略的纯函数实现：
   - equal    等权：所有勾选活动统一给同一个预算。
   - weighted 加权总预算池：Budget_i = max(100, round(TotalPool * Pi / ΣPk))。
"""

from __future__ import annotations

from typing import Any, Dict, List

import numpy as np

from core.parser import natural_sort_key

DEFAULT_MEDIAN_FALLBACK = 2000.0
MIN_BUDGET_FLOOR = 100.0


def build_shop_payload(
    parsed: Dict[str, Any],
    base_budget: float = 100.0,
    min_budget: float = MIN_BUDGET_FLOOR,
) -> Dict[str, Any]:
    """把解析结果转成前端可用的按店铺结构。

    返回：``{shop: {median_price, total_parents, products: [...]}}``，
    products 已按客单均价从高到低排序。
    """
    shops_out: Dict[str, Any] = {}

    for shop_name, bucket in parsed.get("shops", {}).items():
        all_prices: List[float] = bucket.get("all_prices", [])
        groups: Dict[str, Dict[str, Any]] = bucket.get("groups", {})

        median_price = (
            float(np.median(all_prices)) if all_prices else DEFAULT_MEDIAN_FALLBACK
        )

        product_list: List[Dict[str, Any]] = []
        for parent_key, data in groups.items():
            children = data.get("children", [])
            if not children:
                continue

            prices = [p for p in data.get("prices", []) if p and p > 0]
            avg_price = float(np.mean(prices)) if prices else median_price
            ratio = (avg_price / median_price) if median_price > 0 else 1.0

            product_list.append(
                {
                    "parent_sku": parent_key,
                    "product_name": data.get("clean_name") or "商品",
                    "raw_name": data.get("raw_name", ""),
                    "image_url": data.get("image_url", ""),
                    "child_count": len(children),
                    "avg_price": round(avg_price, 2),
                    "median_ratio": round(ratio, 2),
                    "suggested_budget": max(
                        float(min_budget), float(round(base_budget * ratio))
                    ),
                    "children": children,
                }
            )

        # 默认按父 SKU 自然升序（hlx001 < hlx002 < hlx032 < hlx104）
        product_list.sort(key=lambda x: natural_sort_key(x["parent_sku"]))

        shops_out[shop_name] = {
            "median_price": round(median_price, 2),
            "total_parents": len(product_list),
            "products": product_list,
        }

    return shops_out


def assign_budgets(
    products: List[Dict[str, Any]],
    mode: str = "equal",
    value: float = 100.0,
    min_budget: float = MIN_BUDGET_FLOOR,
) -> List[Dict[str, Any]]:
    """按策略为商品列表写入 ``assigned_budget``（后端侧实现，便于脚本化调用）。

    - ``mode='equal'``：全部赋 ``value``。
    - ``mode='weighted'``：``value`` 为总预算池，按客单价占比分配，最低 ``min_budget``。
    """
    selected = [p for p in products if p.get("selected", True)]
    if not selected:
        return products

    if mode == "weighted":
        total_prices = sum(float(p.get("avg_price", 0) or 0) for p in selected)
        n = len(selected)
        for p in selected:
            price = float(p.get("avg_price", 0) or 0)
            weight = (price / total_prices) if total_prices > 0 else (1.0 / n)
            p["assigned_budget"] = max(
                float(min_budget), float(round(float(value) * weight))
            )
    else:
        for p in selected:
            p["assigned_budget"] = float(value)

    return products
