"""Amazon Bulk Operations Ads Generator —— FastAPI 主入口（纯离线名词词库版 v10）。

一键运行：``python main.py``
启动后自动在浏览器打开 http://127.0.0.1:8000

v10 修复（关键）：
- **临时 ID 不再直接用父 SKU**。亚马逊 Bulksheet 2.0 规定 ``Create`` 的实体
  编号必须是「临时 ID」= 任何非数字文本；纯数字会被当成已存在的真实活动 ID，
  报 ``身份证无效 / 此Create操作要求您指定临时"广告活动编号"（任何非数字文本）``，
  整表回滚。v9 生成的 819 行里恰好有 7 个父 SKU 是纯数字（``12`` ``13`` …），
  于是这 7 个活动报错、其余 812 行被级联标记「未处理」。
- 新增 ``_temp_id()`` / ``_plan_temp_ids()``：纯数字加 ``c`` 前缀
  （``12`` -> ``c12``），并保证同一张表内活动 ID 唯一。
- ``_validate_output()`` 增加三项断言：临时 ID 非纯数字、Campaign 编号唯一、
  子行引用的活动编号必须在本表内被定义。

v9 修复：
- **表头与亚马逊官方模板逐字对齐**（31 列）。此前把自动投放表达式写进了
  自造表头 ``商品投放表达式``，亚马逊不认识该列 -> 整列被忽略 ->
  报 ``用户输入无效 / 以下列缺少值：拓展商品投放编号``（5164 行）。
  官方承载「Product Targeting Expression」的列中文名就是 **``拓展商品投放编号``**。
- 修正表头漂移：``商品投放编号`` -> ``商品投放 ID``；``关键词`` -> ``关键词文本``。
- 补齐官方模板缺失列：``母语关键词`` ``母语区域`` ``受众编号`` ``购物者群体占比``
  ``购物者群体类型`` ``站点``（留空）。``ASIN`` 降级为末尾辅助列（亚马逊会忽略）。
- ``百分比`` 为 0 的广告位加成行不再生成（空操作，只会增加被校验的行数）。

v8 修复：
- **修正自动投放四组的行结构**：实体层级 ``Targeting`` -> ``Product Targeting``；
  表达式由 ``targeting_expression = "close-match"`` 改为亚马逊受控裸值；
  ``投放类型`` ``auto`` -> ``Auto``；``广告位`` ``placement top`` -> ``Placement Top``。

与 v7 的差异：
- ``clean_title_noise`` 前置清洗：剥离品牌 / 纯数量规格词（``2個`` / ``3枚`` / ``2個セット``）/ 开头括号。
- **``POST /api/dictionary/rematch``**：导入新词库后，对当前页面全部商品的
  原始日文标题重新跑一遍匹配，**无需重新上传表格**。
"""

from __future__ import annotations

import json
import os
import threading
import webbrowser
from typing import Any, Dict, List

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.budget_engine import build_shop_payload
from core.bulk_generator import generate_bulk_excel
from core.parser import parse_sellfox_excel_multi_shop
from core.translator import noun_matcher

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = FastAPI(title="Amazon Bulk Ads Generator - Offline Noun Dict", version="10.0.0")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class ExportRequest(BaseModel):
    products: List[Dict[str, Any]]
    settings: Dict[str, Any] = {}


class DictEntry(BaseModel):
    ja: str
    zh: str


class RematchItem(BaseModel):
    """单个待重匹配商品。

    - ``key``：回填主键，前端可用「店铺 + 父 SKU」组合以保证跨店铺唯一；缺省回落到 ``parent_sku``。
    - ``parent_sku``：父体 SKU。
    - ``raw_name``：原始日文标题（重匹配的唯一输入）。
    """

    key: str = ""
    parent_sku: str = ""
    raw_name: str = ""


class RematchRequest(BaseModel):
    items: List[RematchItem] = []


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
@app.get("/")
def read_root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/health")
def health():
    return {"status": "ok", "engine": "offline-noun-dict", "version": "10.0.0"}


# ---------------------------------------------------------------------------
# 解析与导出
# ---------------------------------------------------------------------------
@app.post("/api/analyze")
async def analyze_file(file: UploadFile = File(...)):
    """接收赛狐导出 Excel，按店铺分层返回商品聚合结果与预算建议。

    解析异常统一以 400 + 精确原因返回，便于前端直接定位问题。
    """
    try:
        contents = await file.read()
        parsed = parse_sellfox_excel_multi_shop(contents)
        shops = build_shop_payload(parsed, base_budget=100.0)
        if not shops:
            raise ValueError("未从 Excel 中解析出任何有效的商品及店铺数据。")
        return {
            "shops": shops,
            "header_row": parsed.get("header_row", 0),
            "resolved_columns": parsed.get("resolved_columns", {}),
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - 需将底层精确错误透传给前端
        raise HTTPException(status_code=400, detail=f"解析失败：{exc}") from exc


@app.post("/api/generate")
def generate_file(payload: ExportRequest):
    """根据勾选商品与配置生成亚马逊 Bulk Excel 并下载。"""
    buffer = generate_bulk_excel(payload.products, payload.settings)
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=amazon_bulk_ads.xlsx"},
    )


# ---------------------------------------------------------------------------
# 本地纯净名词词库管理
# ---------------------------------------------------------------------------
@app.post("/api/dictionary/import")
async def import_dictionary(file: UploadFile = File(...)):
    """导入 AI 清洗后的纯净中日对照 JSON（``{"日文名词": "中文名词"}``）并持久化。

    返回 ``imported_count``（本次新增/更新条数）与 ``purged_keys``
    （因命中「非纯名词黑名单」而被拒绝的键，便于用户自查）。
    """
    try:
        content = await file.read()
        try:
            data = json.loads(content.decode("utf-8-sig"))
        except UnicodeDecodeError:
            data = json.loads(content.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("导入的词典格式必须为 JSON 对象键值对: {\"日文名词\": \"中文名词\"}")
        result = noun_matcher.update_dictionary(data)
        return {
            "status": "success",
            "imported_count": result["imported"],
            "purged_keys": result["purged"],
            "total_count": result["total"],
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"词库导入失败: {exc}") from exc


@app.get("/api/dictionary/stats")
def get_dict_stats():
    """词库规模总览。"""
    return {"total_count": len(noun_matcher.dictionary)}


@app.get("/api/dictionary")
def get_dictionary():
    """返回词库统计与全部条目（供管理界面使用）。"""
    return {**noun_matcher.stats(), "entries": noun_matcher.list_entries()}


@app.get("/api/dictionary/export")
def export_dictionary():
    """导出当前词库为 JSON 文件（备份 / 二次清洗用）。"""
    payload = json.dumps(noun_matcher.export_dictionary(), ensure_ascii=False, indent=2)
    return JSONResponse(
        content=json.loads(payload),
        headers={"Content-Disposition": "attachment; filename=ja_zh_dict.json"},
    )


@app.post("/api/dictionary")
def upsert_dictionary(entry: DictEntry):
    """新增 / 修改一条词条（立即持久化并清空匹配缓存）。"""
    ja, zh = entry.ja.strip(), entry.zh.strip()
    if not ja or not zh:
        raise HTTPException(status_code=400, detail="日文词与中文词都不能为空")
    noun_matcher.upsert(ja, zh)
    return {"ok": True, "total_count": len(noun_matcher.dictionary)}


@app.delete("/api/dictionary")
def delete_dictionary(ja: str):
    """删除一条词条。"""
    if not noun_matcher.delete(ja):
        raise HTTPException(status_code=404, detail=f"词库中不存在：{ja}")
    return {"ok": True, "total_count": len(noun_matcher.dictionary)}


@app.post("/api/dictionary/reload")
def reload_dictionary():
    """从磁盘重新加载词库（手工编辑 JSON 后手动刷新用）。"""
    return {"ok": True, "total_count": noun_matcher.reload()}


@app.post("/api/dictionary/rematch")
def rematch_names(payload: RematchRequest):
    """用**当前最新词库**对一批商品的原始日文标题重新匹配，返回中文品名映射。

    设计要点（v7）：
    - 纯内存 CPU 扫描，不落盘、不写词库，仅返回结果供前端就地回填；
    - 每个商品同时返回 ``hit`` 标记，区分「词库真实命中」与「兜底占位名」；
    - 回填主键取 ``key``（缺省回落 ``parent_sku``）；``raw_name`` 为空时跳过（不产生噪声键）。

    前端据此实现「导入新词库 → 一键刷新品名 → 无需重新上传表格」。
    """
    matched: Dict[str, str] = {}
    hit_skus: List[str] = []
    miss_skus: List[str] = []

    for item in payload.items:
        key = (item.key or item.parent_sku or "").strip()
        raw = (item.raw_name or "").strip()
        if not key or not raw:
            continue
        zh = noun_matcher.match_noun_only(raw)
        if zh:
            matched[key] = zh
            hit_skus.append(key)
        else:
            # 未收录：仍回填兜底名，但明确标记为 miss，便于用户决定是否手动改名
            matched[key] = noun_matcher.match_first_noun(raw)
            miss_skus.append(key)

    return {
        "matched_names": matched,
        "hit_skus": hit_skus,
        "miss_skus": miss_skus,
        "matched_count": len(matched),
        "hit_count": len(hit_skus),
        "miss_count": len(miss_skus),
        "dict_total": len(noun_matcher.dictionary),
    }


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        pass


if __name__ == "__main__":
    import uvicorn

    host, port = "127.0.0.1", 8000
    url = f"http://{host}:{port}"
    threading.Timer(1.5, _open_browser, args=(url,)).start()
    print(f"\n  Amazon Bulk Ads Generator (Offline Noun Dict) 已启动 -> {url}")
    print(f"  本地词库：{len(noun_matcher.dictionary)} 条  ({noun_matcher.dict_path})\n")
    uvicorn.run("main:app", host=host, port=port, reload=False)
