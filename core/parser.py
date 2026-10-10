"""赛狐 ERP 导出 Excel 自适应解析模块（多店铺版）。

核心能力：
1. **启发式表头嗅探**：扫描前若干行，命中最多的那行即为真实表头行。
2. **模糊别名匹配 + 数据密度择优**：每个字段先按别名库圈定候选列，再按「有效值密度」
   择优——这是修复「价格全兜底 2000、比率全 1x」的关键。
3. **多店铺隔离**：按「店铺」列分层聚合，各店铺独立计算价格中位数。
4. **日文品名中文化**：调用纯离线名词匹配引擎，提取标题中「首个核心实体名词」
   （位置最靠前优先，同位置最长词优先），全程零网络请求。
5. **容错保底**：识别到 SKU 或 ASIN 即视为有效商品；单品 Listing 自动兜底为投放项。
"""

from __future__ import annotations

import io
import re
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import pandas as pd

from core.translator import match_first_nouns_batch

# ---------------------------------------------------------------------------
# 字段别名库（全部小写，匹配时先去除所有空白字符）
# ---------------------------------------------------------------------------
SHOP_ALIASES = ["店铺", "店铺名称", "站点店铺", "shop", "store", "账号", "账户"]
SKU_ALIASES = ["msku", "sku", "平台sku", "商品sku", "sellersku", "本地sku", "商品编码", "本地sku"]
ASIN_ALIASES = ["asin", "平台asin", "标准商品编码", "子asin", "商品asin"]
PARENT_ASIN_ALIASES = ["父asin", "parentasin", "parent asin", "父体asin", "父级asin"]
PRICE_ALIASES = [
    "售价(总价)",
    "售价（总价）",
    "售价",
    "销售价",
    "价格",
    "单价",
    "含税价",
    "标准价",
    "list price",
    "price",
]
NAME_ALIASES = ["品名", "中文品名", "产品名称", "商品名称", "标题", "title", "name"]
TITLE_ALIASES = ["标题", "title", "商品标题"]
TYPE_ALIASES = ["类型", "商品类型", "变体类型", "type"]
IMAGE_ALIASES = ["图片", "商品图片", "主图", "商品主图", "image", "img", "picture"]

HEADER_KEYWORDS = ["asin", "sku", "msku", "价格", "售价", "类型", "品名", "标题", "店铺", "图片"]

DEFAULT_SHOP = "默认店铺"

_IMG_URL_RE = re.compile(r"https?://[^\s\"'|,，;；\)\]]+")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _is_blank(value: Any) -> bool:
    """判断单元格是否为空（None / NaN / 空字符串 / 纯空白）。"""
    if value is None:
        return True
    if isinstance(value, float) and pd.isna(value):
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return str(value).strip() == ""


def _norm_key(text: Any) -> str:
    """归一化用于匹配的字符串：去所有空白 + 转小写。"""
    return re.sub(r"\s+", "", str(text).lower())


def clean_parent_sku(sku_str: Any) -> str:
    """自适应提取父 SKU 核心前缀，过滤尾缀变体标识与批次号。

    例：``ff061-30m-A`` -> ``ff061``；``FF107_2pcs`` -> ``ff107``。
    """
    if _is_blank(sku_str):
        return ""
    s = str(sku_str).strip()
    match = re.match(r"^([a-zA-Z]+0*\d+)", s)
    if match:
        return match.group(1).lower()
    parts = re.split(r"[-_\s]+", s)
    return parts[0].lower() if parts else s.lower()


def clean_product_name(name_str: Any) -> str:
    """过滤特殊符号并以下划线拼接，避免广告活动名称非法。"""
    if _is_blank(name_str):
        return "商品"
    s = str(name_str).strip()
    if not s:
        return "商品"
    cleaned = re.sub(r"[+\-*/\\^&%#@!~`=()\[\]{}|:;\"'<>,.?！？，。、\s\t\n\r]+", "_", s)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned[:35] if cleaned else "商品"


def _extract_price(value: Any) -> float:
    """从单元格中提取数值价格（容忍货币符号、千分位、区间值取首个数字）。"""
    if _is_blank(value):
        return 0.0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).replace(",", "").strip()
    num_match = re.search(r"\d+(?:\.\d+)?", text)
    if not num_match:
        return 0.0
    try:
        return float(num_match.group(0))
    except ValueError:
        return 0.0


def natural_sort_key(text: Any) -> List[Any]:
    """自然排序键：让 hlx001 < hlx002 < hlx032 < hlx104，避免文本排序错位。"""
    return [
        int(chunk) if chunk.isdigit() else chunk.lower()
        for chunk in re.split(r"(\d+)", str(text or ""))
    ]


def _extract_image_url(value: Any) -> str:
    """从「图片」单元格中提取第一个 http(s) 图片链接。"""
    if _is_blank(value):
        return ""
    match = _IMG_URL_RE.search(str(value))
    return match.group(0) if match else ""


# ---------------------------------------------------------------------------
# 表头嗅探与列定位
# ---------------------------------------------------------------------------
def find_best_header_row(file_bytes: bytes, scan_rows: int = 6) -> int:
    """自动扫描前若干行，根据命中特征词的密度判定真实表头所在行。"""
    try:
        preview_df = pd.read_excel(io.BytesIO(file_bytes), header=None, nrows=scan_rows)
    except Exception:
        return 0
    if preview_df.empty:
        return 0

    best_row, max_hits = 0, -1
    for idx, row in preview_df.iterrows():
        row_str = " ".join(str(v).lower() for v in row.values if not _is_blank(v))
        hits = sum(1 for kw in HEADER_KEYWORDS if kw in row_str)
        if hits > max_hits:
            max_hits, best_row = hits, int(idx)
    return best_row


def match_column(columns: List[str], candidate_keywords: List[str]) -> Optional[str]:
    """模糊匹配单个列名：先精确（去空白）匹配，再包含匹配。"""
    matches = match_columns(columns, candidate_keywords)
    return matches[0] if matches else None


def match_columns(columns: List[str], candidate_keywords: List[str]) -> List[str]:
    """返回所有命中的列，按「别名优先级 + 原列顺序」排序。"""
    cleaned_cols = [(col, _norm_key(col)) for col in columns]
    found: List[str] = []

    # 1. 精确匹配（按别名优先级）
    for kw in candidate_keywords:
        target = _norm_key(kw)
        for orig, cleaned in cleaned_cols:
            if cleaned == target and orig not in found:
                found.append(orig)

    # 2. 包含匹配（按别名优先级）
    for kw in candidate_keywords:
        target = _norm_key(kw)
        if not target:
            continue
        for orig, cleaned in cleaned_cols:
            if target in cleaned and orig not in found:
                found.append(orig)
    return found


def _pick_by_density(
    df: pd.DataFrame, candidates: List[str], scorer: Callable[[Any], float]
) -> Optional[str]:
    """在候选列中挑选「有效值最多」的一列，避免选中空列/占位列。"""
    best_col, best_score = None, 0.0
    for col in candidates:
        try:
            score = float(sum(scorer(v) for v in df[col].tolist()))
        except Exception:
            continue
        if score > best_score:
            best_col, best_score = col, score
    return best_col


# ---------------------------------------------------------------------------
# 主解析流程
# ---------------------------------------------------------------------------
def parse_sellfox_excel_multi_shop(
    file_content: Any, enable_translation: bool = True
) -> Dict[str, Any]:
    """解析赛狐导出 Excel，按店铺分层聚合父子变体数据。

    Args:
        file_content: Excel 字节流 / 文件对象 / 路径。
        enable_translation: **已废弃**（v6 起全程离线，不再发起任何网络请求），
            保留该形参仅为兼容旧调用方。
    """
    if isinstance(file_content, (bytes, bytearray)):
        file_bytes = bytes(file_content)
    elif hasattr(file_content, "read"):
        file_bytes = file_content.read()
    else:
        file_bytes = file_content

    # 1. 自动嗅探表头行
    header_idx = find_best_header_row(file_bytes)
    df = pd.read_excel(io.BytesIO(file_bytes), header=header_idx)
    df.columns = [str(c).strip() for c in df.columns]
    cols = list(df.columns)

    # 2. 定位各字段列（价格/SKU/ASIN 按数据密度择优）
    shop_col = match_column(cols, SHOP_ALIASES)
    parent_asin_col = match_column(cols, PARENT_ASIN_ALIASES)

    sku_col = _pick_by_density(
        df, match_columns(cols, SKU_ALIASES), lambda v: 0.0 if _is_blank(v) else 1.0
    )
    asin_candidates = [
        c for c in match_columns(cols, ASIN_ALIASES) if c != parent_asin_col
    ]
    asin_col = _pick_by_density(
        df, asin_candidates, lambda v: 0.0 if _is_blank(v) else 1.0
    )
    price_col = _pick_by_density(
        df, match_columns(cols, PRICE_ALIASES), lambda v: 1.0 if _extract_price(v) > 0 else 0.0
    )

    name_candidates = match_columns(cols, NAME_ALIASES)
    title_col = match_column(cols, TITLE_ALIASES)
    name_col = _pick_by_density(
        df, name_candidates, lambda v: 0.0 if _is_blank(v) else 1.0
    )
    type_col = match_column(cols, TYPE_ALIASES)
    img_col = match_column(cols, IMAGE_ALIASES)

    # 3. 底线校验：至少识别到 SKU 或 ASIN 之一
    if not sku_col and not asin_col:
        shown = ", ".join(str(c) for c in cols[:10])
        raise ValueError(
            f"无法识别文件中的商品标识列（SKU / ASIN 均未找到）。"
            f"当前识别到的表头行第 {header_idx + 1} 行，列为：{shown}"
        )

    # 4. 逐行聚合（按店铺分层）
    raw_shops: Dict[str, Dict[str, Any]] = {}

    for _, row in df.iterrows():
        shop = (
            str(row.get(shop_col)).strip()
            if shop_col and not _is_blank(row.get(shop_col))
            else DEFAULT_SHOP
        )
        sku = "" if not sku_col or _is_blank(row.get(sku_col)) else str(row.get(sku_col)).strip()
        asin = "" if not asin_col or _is_blank(row.get(asin_col)) else str(row.get(asin_col)).strip()
        row_type = (
            "" if not type_col or _is_blank(row.get(type_col)) else str(row.get(type_col)).strip()
        )
        img_url = _extract_image_url(row.get(img_col)) if img_col else ""

        # 品名优先「品名」列，为空回退「标题」列
        raw_name = ""
        for col in (name_col, title_col):
            if col and not _is_blank(row.get(col)):
                raw_name = str(row.get(col)).strip()
                break

        if not sku and not asin:
            continue

        price = _extract_price(row.get(price_col)) if price_col else 0.0

        shop_bucket = raw_shops.setdefault(shop, {"groups": {}, "all_prices": []})
        if price > 0:
            shop_bucket["all_prices"].append(price)

        parent_key = clean_parent_sku(sku if sku else asin) or (sku or asin).lower()
        groups = shop_bucket["groups"]

        group = groups.setdefault(
            parent_key,
            {
                "parent_sku": parent_key,
                "raw_name": raw_name,
                "image_url": "",
                "children": [],
                "prices": [],
            },
        )
        # 保留更短、更干净的原名（通常是最具代表性的品名）
        if raw_name and (
            not group["raw_name"] or len(raw_name) < len(group["raw_name"])
        ):
            group["raw_name"] = raw_name
        # 图片：父体行优先，否则取第一个有图的子体
        if img_url and not group["image_url"]:
            group["image_url"] = img_url

        # 纯父体占位行不参与投放
        is_parent_row = ("父" in row_type and "子" not in row_type) or (
            sku.lower() == parent_key and not asin
        )
        if not is_parent_row:
            group["children"].append(
                {
                    "msku": sku if sku else asin,
                    "asin": asin if asin else sku,
                    "price": price,
                }
            )
            if price > 0:
                group["prices"].append(price)

    if not raw_shops:
        raise ValueError("未能从表格中解析出有效商品数据，请检查表格内容是否为空。")

    # 5. 批量提取「首个核心实体名词」（纯本地词库扫描，零网络请求）
    #    优先用品名，品名为空则回退到完整日文标题 / 父 SKU 做全文名词嗅探
    all_targets: List[str] = []
    for bucket in raw_shops.values():
        for g in bucket["groups"].values():
            target = g["raw_name"] or g["parent_sku"]
            if target:
                all_targets.append(target)
    name_map = match_first_nouns_batch(all_targets)

    # 6. 组装最终结构
    shops: Dict[str, Dict[str, Any]] = {}
    for shop_name, bucket in raw_shops.items():
        groups_out: Dict[str, Dict[str, Any]] = {}
        for parent_key, g in bucket["groups"].items():
            raw_name = g["raw_name"]
            target_text = raw_name or parent_key
            zh_name = name_map.get(target_text) or clean_product_name(target_text)

            children = g["children"]
            if not children:
                # 单品 Listing 兜底
                fallback_price = (
                    float(np.mean(g["prices"])) if g["prices"] else 0.0
                )
                children = [
                    {"msku": parent_key, "asin": parent_key.upper(), "price": fallback_price}
                ]

            groups_out[parent_key] = {
                "parent_sku": parent_key,
                "clean_name": zh_name,
                "raw_name": raw_name,
                "image_url": g.get("image_url", ""),
                "children": children,
                "prices": g["prices"],
            }

        if groups_out:
            shops[shop_name] = {"groups": groups_out, "all_prices": bucket["all_prices"]}

    if not shops:
        raise ValueError("未能从表格中聚合出有效父子变体数据，请检查表格内容是否为空。")

    return {
        "shops": shops,
        "columns": cols,
        "header_row": header_idx,
        "resolved_columns": {
            "shop": shop_col,
            "price": price_col,
            "sku": sku_col,
            "asin": asin_col,
            "parent_asin": parent_asin_col,
            "name": name_col,
            "title": title_col,
            "type": type_col,
            "image": img_col,
        },
    }
