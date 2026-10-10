# -*- coding: utf-8 -*-
"""生成「赛狐导出」合成样本，供验收测试使用。

为什么需要它
--------------------------------------------------------------------
真实业务导出表（含店铺名、SKU、价格）**不会**进仓库（见根目录 ``.gitignore``）。
但 ``tests/test_2_end_to_end.py`` / ``tests/test_4_http_smoke.py`` 需要一个
「多店铺 + 父体/子体 + 日文标题 + 千分位价格」的输入来跑端到端链路。

因此这里生成一份**全合成**样本：店铺名、ASIN、MSKU、SKU 全部是假数据，
日文标题只用通用品类名词（不含任何真实 Listing 信息）。

形状（与真实导出保持一致，便于回归）
--------------------------------------------------------------------
- 4 个店铺
- 44 个父体（12 / 11 / 11 / 10）
- 每个父体 1~4 个子体（含「单子体」边界）
- 列：店铺 / 类型 / 父ASIN / ASIN / MSKU / 标题 / 品名 / SKU / 售价
- ``售价`` 用带千分位的字符串（如 ``"3,480"``），模拟赛狐真实导出格式

用法
--------------------------------------------------------------------
    python tools/make_demo_fixture.py

输出：``samples/fixtures/sellfox_export_demo.xlsx``
"""

from __future__ import annotations

import os

import openpyxl

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(BASE, "samples", "fixtures")
OUT_PATH = os.path.join(OUT_DIR, "sellfox_export_demo.xlsx")

HEADER = ["店铺", "类型", "父ASIN", "ASIN", "MSKU", "标题", "品名", "SKU", "售价"]

# 每个店铺的父体数量（合计 44，与既有回归断言保持一致）
SHOP_PLAN = [
    ("示例店铺A-JP", 12),
    ("示例店铺B-JP", 11),
    ("示例店铺C-JP", 11),
    ("示例店铺D-JP", 10),
]

# 通用日文品类名词（均为词库收录的通用名词，不含任何真实 Listing 信息）
NOUNS = [
    ("冷却加湿器", ["スプレー式冷却加湿器", "スプレー式冷却加湿器 業務用", "超音波加湿器 大容量"]),
    ("折叠桌", ["折りたたみテーブル", "折りたたみ式テーブル 高さ調節", "折りたたみテーブル 軽量"]),
    ("菜刀架", ["ステンレス包丁立て", "包丁立て 木製", "包丁スタンド 大容量"]),
    ("收纳箱", ["収納ボックス", "収納ボックス 折りたたみ", "紙製収納ボックス 3個セット"]),
    ("置物架", ["スチールラック", "メタルラック 4段", "スチールラック 耐荷重"]),
    ("冰淇淋勺", ["アイスクリームスクープ", "アイスクリームスクープ 業務用"]),
    ("马克杯", ["マグカップ", "マグカップ 陶器", "マグカップ 保温"]),
    ("茶壶", ["ティーポット", "急須 陶器", "ティーポット 耐熱"]),
    ("塑料桶", ["ポリタンク", "水タンク 大容量", "貯水タンク 折りたたみ"]),
    ("风铃", ["風鈴", "風鈴 ガラス製"]),
    ("研钵", ["すり鉢", "すり鉢 大サイズ"]),
    ("蛋糕模具", ["ケーキ型", "ケーキ型 アルミ", "パイ皿 ステンレス"]),
    ("折叠椅", ["折りたたみチェア", "折りたたみチェア 軽量", "アウトドアチェア"]),
    ("水桶", ["バケツ", "バケツ 大容量"]),
    ("沥水器", ["湯切り", "湯切り ステンレス"]),
    ("滑轨", ["スライドレール", "スライドレール 2本セット"]),
    ("打蛋器", ["泡立て器", "泡立て器 ステンレス"]),
    ("竹签", ["竹串", "使い捨て竹串 100本"]),
    ("保鲜盒", ["フードストッカー", "密閉容器 3個セット"]),
    ("洒水壶", ["じょうろ", "じょうろ 大容量"]),
]

# 价格序列（刻意包含大小差异 + 千分位，用于检验中位数 / 加权预算）
PRICES = [
    "1,280", "1,980", "2,480", "2,980", "3,480", "3,980", "4,480",
    "5,980", "6,480", "7,980", "9,800", "12,800",
]


def _parent_asin(seq: int) -> str:
    """合成父体 ASIN（10 位，B0PARENT + 两位序号）。"""
    return f"B0PARENT{seq:02d}"


def _child_asin(seq: int) -> str:
    """合成子体 ASIN（10 位，B0CHILD + 三位序号）——与父体号段完全分离，避免重号。"""
    return f"B0CHILD{seq:03d}"


def build_rows() -> list:
    rows = []
    parent_seq = 0
    asin_seq = 0
    price_i = 0

    for shop, n_parent in SHOP_PLAN:
        shop_tag = shop.split("-")[0][-1]  # A / B / C / D
        for k in range(1, n_parent + 1):
            parent_seq += 1
            zh, titles = NOUNS[parent_seq % len(NOUNS)]
            parent_sku = f"dm{shop_tag.lower()}{k:03d}"
            parent_asin = _parent_asin(parent_seq)
            # 子体数在 1~4 之间循环，确保覆盖「单子体」边界
            n_child = 1 + (parent_seq % 4)
            # 父体行：ASIN / MSKU / 售价 留空，与赛狐真实导出一致
            rows.append([shop, "父体", parent_asin, None, parent_sku, titles[0], None, parent_sku, None])
            for c in range(1, n_child + 1):
                asin_seq += 1
                child_sku = f"{parent_sku}-{c:02d}"
                title = titles[c % len(titles)]
                price = PRICES[price_i % len(PRICES)]
                price_i += 1
                rows.append([shop, "子体", parent_asin, _child_asin(asin_seq), child_sku, title, zh, child_sku, price])
    return rows


def main() -> None:
    rows = build_rows()
    n_parent = sum(1 for r in rows if r[1] == "父体")
    n_child = sum(1 for r in rows if r[1] == "子体")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(HEADER)
    for r in rows:
        ws.append(r)

    os.makedirs(OUT_DIR, exist_ok=True)
    wb.save(OUT_PATH)
    print(f"已写出 {OUT_PATH}")
    print(f"  店铺 {len(SHOP_PLAN)} 个 | 父体 {n_parent} 个 | 子体 {n_child} 个 | 总行 {len(rows) + 1}")


if __name__ == "__main__":
    main()
