# -*- coding: utf-8 -*-
"""v2.13 验收测试：店铺日报（会话 / 广告点击 / 花费÷会话）。

只测纯逻辑，不碰网络：
  1. 日期区间展开（含反向入参）
  2. 数值兜底（None / "" / 非法字符串 → 0）
  3. 表格结构：4 项派生指标、A→Z 排序、天数正确
  4. 会话为 0 时比值给 None（前端显示「—」，不做除零）
  5. include_empty 能把没有数据的店铺补成占位行
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.stdout.reconfigure(encoding="utf-8")

import core.shop_daily as sd  # noqa: E402

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
print("v2.13 验收：店铺日报")
print("=" * 78)

# ---------------------------------------------------------------- 1. 日期区间
print("\n[1] 日期区间展开")
days = sd._date_range("2026-11-01", "2026-11-03")
check("闭区间 3 天", days == ["2026-11-01", "2026-11-02", "2026-11-03"], str(days))
check("单日 = 1 天", sd._date_range("2026-11-05", "2026-11-05") == ["2026-11-05"])
check("反向入参自动纠正", sd._date_range("2026-11-03", "2026-11-01") == days)
check("跨月连续", sd._date_range("2026-10-31", "2026-11-01") == ["2026-10-31", "2026-11-01"])

# ---------------------------------------------------------------- 2. 数值兜底
print("\n[2] 数值兜底")
check("None → 0", sd._num(None) == 0)
check("空串 → 0", sd._num("") == 0)
check("非法串 → 0", sd._num("abc") == 0)
check("字符串数字可解析", sd._num("415") == 415)
check("int 转换", sd._num("415", int) == 415)

# ---------------------------------------------------------------- 3. 表格结构
print("\n[3] 表格结构与派生指标")

FAKE = [
    # 赖皮蛇：点击 415 / 会话 852 / 花费 14785.08
    {"shop_id": "509513", "report_date": "2026-11-13", "shop_name": "赖皮蛇-JP",
     "ad_clicks": 415, "ad_cost": 14785.08, "sessions": 852, "page_views": 1059,
     "updated_at": "2026-11-16T10:00:00"},
    # 枳枳商务：会话 0（当天业务报告还没到）
    {"shop_id": "705831", "report_date": "2026-11-13", "shop_name": "枳枳商务-JP",
     "ad_clicks": 300, "ad_cost": 9000.0, "sessions": 0, "page_views": 0,
     "updated_at": "2026-11-16T10:00:00"},
]

sd.query_range = lambda start, end: FAKE  # noqa: E731
sd.db.list_shops = lambda *a, **k: [  # noqa: E731
    {"shop_id": "509513", "shop_name": "赖皮蛇-JP", "alias": "O赖皮蛇-JP"},
    {"shop_id": "705831", "shop_name": "枳枳商务-JP", "alias": "P枳枳商务-JP"},
    {"shop_id": "563248", "shop_name": "三头牛-JP", "alias": "A三头牛-JP"},
]

table = sd.build_month_table("2026-11")
check("11 月共 30 天", len(table["days"]) == 30, str(len(table["days"])))
check("默认只返回有数据店铺", len(table["shops"]) == 2, str(len(table["shops"])))
check("按别名 A→Z 排序",
      [s["display_name"] for s in table["shops"]] == ["O赖皮蛇-JP", "P枳枳商务-JP"],
      str([s["display_name"] for s in table["shops"]]))

lps = table["shops"][0]
cell = lps["cells"]["2026-11-13"]
check("点击 / 会话原样保留", cell["clicks"] == 415 and cell["sessions"] == 852, str(cell))
check("点击÷会话 = (852-415)/852",
      abs(cell["ratio"] - round((852 - 415) / 852, 4)) < 1e-9, str(cell["ratio"]))
check("花费÷会话 = 14785.08/852",
      cell["cost_per_session"] == round(14785.08 / 852, 2), str(cell["cost_per_session"]))

# ---------------------------------------------------------------- 4. 除零保护
print("\n[4] 会话为 0 的除零保护")
zero = table["shops"][1]["cells"]["2026-11-13"]
check("会话 0 → ratio 为 None", zero["ratio"] is None, str(zero["ratio"]))
check("会话 0 → 花费÷会话为 None", zero["cost_per_session"] is None, str(zero["cost_per_session"]))

# ---------------------------------------------------------------- 5. 占位行
print("\n[5] 显示无数据店铺")
table_all = sd.build_month_table("2026-11", include_empty=True)
names = [s["display_name"] for s in table_all["shops"]]
check("补齐到 3 家店", len(names) == 3, str(names))
check("无数据店铺按别名排在最前", names[0] == "A三头牛-JP", str(names))
empty = [s for s in table_all["shops"] if s["display_name"] == "A三头牛-JP"][0]
check("占位店铺 cells 为空", empty["cells"] == {}, str(empty["cells"]))

# ---------------------------------------------------------------- 6. 月份天数
print("\n[6] 月份天数")
check("2026-02 有 28 天", len(sd.build_month_table("2026-02")["days"]) == 28)

print("\n" + "-" * 78)
print(f"结果：PASS {OK}  FAIL {FAIL}")
sys.exit(0 if FAIL == 0 else 1)
