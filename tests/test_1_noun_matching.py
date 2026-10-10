# -*- coding: utf-8 -*-
"""v6 验收测试一：纯离线名词匹配引擎。

覆盖规格第 5 节五项效果验证标准：
  1. 菜刀架测试   —— ``包丁立て`` 长词优先，不被 ``包丁`` 截断
  2. 陈列架测试   —— ``店舗用陳列架子大型連結式`` 全文命中首个实体名词
  3. 折叠桌测试   —— 修饰词 ``折りたたみ`` 不得作为品名
  4. 零调用限制   —— 全程无任何网络请求
  5. 性能         —— 1300+ Listing 在 1 秒内完成
"""
import os
import socket
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.stdout.reconfigure(encoding="utf-8")

from core.translator import (  # noqa: E402
    NON_NOUN_BLACKLIST,
    NounDictionaryMatcher,
    noun_matcher,
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
print("v6 验收一：纯离线名词匹配引擎")
print("=" * 78)
print(f"词库条目 {len(noun_matcher.dictionary)} · 黑名单 {len(NON_NOUN_BLACKLIST)} 条 · "
      f"本次清洗 {len(noun_matcher.purged_keys)} 个非纯名词")

# ---------------------------------------------------------------- 标准 1/2/3
print("\n[1-3] 规格第 5 节三项命名验收")
CASES = [
    # (标题, 期望中文, 说明)
    ("ステンレス包丁立て 大容量", "菜刀架", "标准1 长词优先：包丁立て > 包丁"),
    ("包丁立て", "菜刀架", "标准1 最短形态"),
    ("包丁", "菜刀", "标准1 对照：单独包丁仍是菜刀"),
    ("店舗用陳列架子大型連結式", "陈列架", "标准2 全文嗅探：过滤 店舗用/大型/連結式"),
    ("店舗用陳列棚", "陈列架", "标准2 同义长词"),
    ("折りたたみテーブル", "折叠桌", "标准3 折叠桌：不命名为「折叠」"),
    ("折りたたみ テーブル", "折叠桌", "标准3 含空格容错"),
    ("キャンプ用折りたたみテーブル", "折叠桌", "标准3 场景词+修饰词双重过滤"),
]
for text, expect, note in CASES:
    got = noun_matcher.match_first_noun(text)
    check(f"{text}", got == expect, f"-> {got!r}  （{note}）")

print("\n[3b] 修饰词/功能词不得作为品名")
MODIFIER_CASES = [
    ("折りたたみ", "折叠"),
    ("収納", "收纳"),
    ("充電", "充电"),
    ("キャンプ", "露营用品"),
    ("アウトドア", "户外用品"),
]
for text, banned in MODIFIER_CASES:
    got = noun_matcher.match_first_noun(text)
    check(f"{text} 不映射为「{banned}」", got != banned, f"实际 {got!r}")
# 关键断言：这些词必须已被清出词库（保冷/大型 中日文写法相同，故只查成员资格）
for banned_key in ("折りたたみ", "折り畳み", "保冷", "収納", "充電", "大型", "小型",
                   "連結式", "キャンプ", "アウトドア", "ゴルフ", "ペット"):
    check(f"「{banned_key}」已从词库剔除", banned_key not in noun_matcher.dictionary)
check("黑名单外的纯名词仍在库", "オットマン" in noun_matcher.dictionary and "テーブル" in noun_matcher.dictionary)
check("长名词仍在库", "包丁立て" in noun_matcher.dictionary and "折りたたみテーブル" in noun_matcher.dictionary)

print("\n[3c] 品牌前缀剥离与位置优先")
BRAND_CASES = [
    ("FryWare_オットマン 大型 業務用", "脚凳"),
    ("DOHERA システム手帳", "手账本"),
    ("2WAY ダイニングテーブル", "餐桌"),
    ("テーブル オットマン", "桌子"),      # 位置优先：先出现者胜
    ("オットマン テーブル", "脚凳"),
]
for text, expect in BRAND_CASES:
    got = noun_matcher.match_first_noun(text)
    check(f"{text}", got == expect, f"-> {got!r}")

print("\n[3c-2] 修饰后缀跳过（式/用/型/製/付/向/対）")
SUFFIX_CASES = [
    ("スプレー式冷却加湿器", "加湿器", "スプレー式 是修饰成分"),
    ("テーブル用ラック", "置物架", "テーブル用 是修饰成分"),
    ("オットマン用カバー", "保护套", "オットマン用 是修饰成分"),
    ("ボクシング力測定器", "测量仪", "类目词被剔除后仍能命中真实名词"),
    ("折りたたみ式テーブル", "折叠桌", "折りたたみ式"),
]
for text, expect, note in SUFFIX_CASES:
    got = noun_matcher.match_first_noun(text)
    check(f"{text}", got == expect, f"-> {got!r}  （{note}）")

print("\n[3d] 兜底行为")
check("空输入 -> 商品", noun_matcher.match_first_noun("") == "商品")
check("None -> 商品", noun_matcher.match_first_noun(None) == "商品")
unknown = noun_matcher.match_first_noun("謎の物体xyz")
check("未命中 -> 首词兜底（非空）", bool(unknown) and unknown != "商品", f"{unknown!r}")

# ---------------------------------------------------------------- 标准 4
print("\n[4] 零网络调用：屏蔽 socket 后仍可正常匹配")


class _BlockedSocket(socket.socket):
    def connect(self, *a, **k):  # noqa: D102
        raise AssertionError("检测到网络调用！v6 应为纯离线")

    def connect_ex(self, *a, **k):  # noqa: D102
        raise AssertionError("检测到网络调用！v6 应为纯离线")


_orig_socket = socket.socket
socket.socket = _BlockedSocket
try:
    offline_results = [noun_matcher.match_first_noun(t) for t, _, _ in CASES]
    check("断网状态下匹配全部成功", all(offline_results), f"{len(offline_results)} 条")
finally:
    socket.socket = _orig_socket

# 源码层面确认无网络库依赖
src = open(os.path.join(BASE, "core", "translator.py"), encoding="utf-8").read()
for lib in ("requests", "urllib", "http.client", "deep_translator", "socket", "aiohttp"):
    check(f"translator.py 未引入 {lib}", lib not in src.replace("屏蔽", ""))

# ---------------------------------------------------------------- 标准 5
print("\n[5] 性能：1300+ Listing 匹配耗时")
SAMPLE_TITLES = [
    "ステンレス包丁立て 大容量 業務用 キッチン収納",
    "店舗用陳列架子大型連結式 ディスプレイ 什器",
    "折りたたみテーブル アウトドア キャンプ 軽量 コンパクト",
    "FryWare_オットマン 高さ調節 フットレスト カバー付き",
    "ダイニング テーブル 4人掛け 木製 ナチュラル",
    "オフィスチェア メッシュ ランバーサポート 静音キャスター",
    "収納ボックス 折りたたみ 大容量 衣類 おしゃれ",
    "クーラーボックス 保冷 25L アウトドア 釣り 車載",
    "まな板 抗菌 食洗機対応 カッティングボード",
    "システム手帳 バイブルサイズ 革 リフィル付き",
]
# 生成 1300 条「互不相同」的标题，避免命中缓存导致测不准
N = 1300
batch = [f"{SAMPLE_TITLES[i % len(SAMPLE_TITLES)]} {i:04d}" for i in range(N)]
check("样本互不相同", len(set(batch)) == N, f"{len(set(batch))}/{N}")

noun_matcher._cache.clear()          # 冷缓存
t0 = time.perf_counter()
out = [noun_matcher.match_first_noun(t) for t in batch]
dt = time.perf_counter() - t0
print(f"     冷缓存 {N} 条耗时 {dt * 1000:.1f} ms  ({N / dt:,.0f} 条/秒)")
check("1300+ Listing 在 1 秒内完成", dt < 1.0, f"{dt * 1000:.1f} ms")
check("全部返回非空结果", all(out), f"示例 {out[:4]}")

t0 = time.perf_counter()
for t in batch:
    noun_matcher.match_first_noun(t)
dt_warm = time.perf_counter() - t0
print(f"     热缓存 {N} 条耗时 {dt_warm * 1000:.1f} ms")
check("热缓存更快", dt_warm <= dt, f"{dt_warm * 1000:.1f} ms")

# ---------------------------------------------------------------- 索引正确性
print("\n[6] 首字符倒排索引一致性")
m2 = NounDictionaryMatcher(dict_path=os.path.join(BASE, "data", "ja_zh_dict.json"))
total_indexed = sum(len(v) for v in m2._index.values())
check("索引覆盖全部词条", total_indexed == len(m2.dictionary), f"{total_indexed}/{len(m2.dictionary)}")
check("每个首字符桶内按长度降序",
      all(all(len(v[i]) >= len(v[i + 1]) for i in range(len(v) - 1))
          for v in m2._index.values() if len(v) > 1))

print("\n" + "=" * 78)
print(f"结果：PASS {OK}  FAIL {FAIL}")
print("=" * 78)
sys.exit(1 if FAIL else 0)
