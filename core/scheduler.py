"""APScheduler 定时任务管理（v2.1：可配置计划 + 手动触发）。

两大定时任务：
1. **商品增量同步** ``product_sync``：默认每周一 03:00
2. **搜索词每日同步** ``search_sync``：默认每日 02:00（取昨日 T-1 数据）

v2.1 增强
---------
- **计划可配置**：星期 / 小时 / 分钟 / 是否启用，落盘 ``data/scheduler_config.json``，
  重启后自动恢复；保存后立即生效，无需重启服务。
- **手动触发**：``run_job_now()`` 在后台线程立即跑一次，不阻塞 HTTP 请求；
  同一任务正在跑时拒绝重复触发（返回 busy），避免并发打爆赛狐接口。
- **运行状态**：每个任务的 ``running`` / ``last_run`` / ``last_result`` / ``last_error``
  可通过 ``get_scheduler_status()`` 查询，前端轮询展示。

时区固定 ``Asia/Tokyo``（日本站，与赛狐站点时区一致）。
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from core import sync_service

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配置持久化
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, "data", "scheduler_config.json")

TIMEZONE = "Asia/Tokyo"

# 任务键（前后端共用）
JOB_PRODUCT = "product_sync"
JOB_SEARCH = "search_sync"

JOB_LABELS = {
    JOB_PRODUCT: "商品增量同步",
    JOB_SEARCH: "搜索词每日同步",
}

# 合法星期值（APScheduler cron day_of_week）
VALID_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

DAY_LABELS = {
    "mon": "周一", "tue": "周二", "wed": "周三", "thu": "周四",
    "fri": "周五", "sat": "周六", "sun": "周日",
}

# 默认计划
DEFAULT_CONFIG: Dict[str, Dict[str, Any]] = {
    JOB_PRODUCT: {
        "enabled": True,
        "day_of_week": "mon",
        "hour": 3,
        "minute": 0,
    },
    JOB_SEARCH: {
        "enabled": True,
        "hour": 2,
        "minute": 0,
    },
}


def load_config() -> Dict[str, Dict[str, Any]]:
    """读取计划配置；文件缺失或损坏时回落默认值。"""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝
    if not os.path.exists(CONFIG_FILE):
        return cfg
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            for key in cfg:
                if isinstance(saved.get(key), dict):
                    cfg[key].update(saved[key])
    except (json.JSONDecodeError, IOError) as exc:
        logger.warning("[Scheduler] 配置文件读取失败，使用默认计划: %s", exc)
    return _sanitize(cfg)


def save_config(cfg: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """校验并落盘计划配置。"""
    clean = _sanitize(cfg)
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    return clean


def _sanitize(cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """把任意输入规整成合法的计划配置（越界值夹取，类型强转）。"""
    out: Dict[str, Dict[str, Any]] = {}

    def _int_in(value: Any, lo: int, hi: int, default: int) -> int:
        try:
            n = int(value)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, n))

    # 商品同步（每周）
    prod_src = cfg.get(JOB_PRODUCT) or {}
    dow = str(prod_src.get("day_of_week", "mon")).lower()
    if dow not in VALID_DAYS:
        dow = "mon"
    out[JOB_PRODUCT] = {
        "enabled": bool(prod_src.get("enabled", True)),
        "day_of_week": dow,
        "hour": _int_in(prod_src.get("hour", 3), 0, 23, 3),
        "minute": _int_in(prod_src.get("minute", 0), 0, 59, 0),
    }

    # 搜索词同步（每日）
    search_src = cfg.get(JOB_SEARCH) or {}
    out[JOB_SEARCH] = {
        "enabled": bool(search_src.get("enabled", True)),
        "hour": _int_in(search_src.get("hour", 2), 0, 23, 2),
        "minute": _int_in(search_src.get("minute", 0), 0, 59, 0),
    }
    return out


# ---------------------------------------------------------------------------
# 运行时状态
# ---------------------------------------------------------------------------
_scheduler: Optional[BackgroundScheduler] = None
_state_lock = threading.Lock()

_JOB_STATE: Dict[str, Dict[str, Any]] = {
    JOB_PRODUCT: {"running": False, "last_run": None, "last_result": None, "last_error": None},
    JOB_SEARCH: {"running": False, "last_run": None, "last_result": None, "last_error": None},
}


def _set_state(job_key: str, **kwargs) -> None:
    with _state_lock:
        _JOB_STATE.setdefault(job_key, {}).update(kwargs)


def _is_running(job_key: str) -> bool:
    with _state_lock:
        return bool(_JOB_STATE.get(job_key, {}).get("running"))


# ---------------------------------------------------------------------------
# 任务实现
# ---------------------------------------------------------------------------
def _summarize(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把逐店铺结果汇总成一行摘要。"""
    ok = [r for r in results if "error" not in r]
    failed = [r for r in results if "error" in r]
    return {
        "shops_total": len(results),
        "shops_ok": len(ok),
        "shops_failed": len(failed),
        "errors": [{"shop_id": r.get("shop_id"), "error": r["error"]} for r in failed][:10],
        "details": results,
    }


def _job_sync_products() -> Dict[str, Any]:
    """商品增量同步：拉取全店铺在线商品，仅入库新上架 SKU。"""
    logger.info("[Scheduler] 开始执行商品增量同步...")
    results = sync_service.sync_products_all_shops()
    summary = _summarize(results)
    summary["new_inserted"] = sum(int(r.get("new_inserted") or 0) for r in results)
    summary["total_fetched"] = sum(int(r.get("total_fetched") or 0) for r in results)
    logger.info(
        "[Scheduler] 商品同步完成: 店铺 %s/%s 成功, 拉取 %s, 新增 %s",
        summary["shops_ok"], summary["shops_total"],
        summary["total_fetched"], summary["new_inserted"],
    )
    return summary


def _job_sync_search_terms() -> Dict[str, Any]:
    """搜索词每日同步：拉取昨日（T-1）全店铺搜索词报告。"""
    logger.info("[Scheduler] 开始执行搜索词每日同步...")
    results = sync_service.sync_search_terms_all_shops()
    summary = _summarize(results)
    summary["search_terms_inserted"] = sum(
        int(r.get("search_terms_inserted") or 0) for r in results
    )
    summary["search_terms_fetched"] = sum(
        int(r.get("search_terms_fetched") or 0) for r in results
    )
    logger.info(
        "[Scheduler] 搜索词同步完成: 店铺 %s/%s 成功, 拉取 %s, 入库 %s",
        summary["shops_ok"], summary["shops_total"],
        summary["search_terms_fetched"], summary["search_terms_inserted"],
    )
    return summary


_JOB_FUNCS = {
    JOB_PRODUCT: _job_sync_products,
    JOB_SEARCH: _job_sync_search_terms,
}


def _run_wrapped(job_key: str) -> None:
    """带状态记录的任务包装器（定时与手动共用）。"""
    _set_state(
        job_key,
        running=True,
        last_error=None,
        last_run=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    try:
        result = _JOB_FUNCS[job_key]()
        _set_state(job_key, last_result=result, running=False)
    except Exception as exc:  # noqa: BLE001 - 需记录任意失败原因给前端
        logger.error("[Scheduler] 任务 %s 异常: %s", job_key, exc, exc_info=True)
        _set_state(job_key, last_error=str(exc), running=False)


# ---------------------------------------------------------------------------
# 调度器生命周期
# ---------------------------------------------------------------------------
def _build_trigger(job_key: str, conf: Dict[str, Any]) -> CronTrigger:
    """按配置构建 cron 触发器。"""
    if job_key == JOB_PRODUCT:
        return CronTrigger(
            day_of_week=conf["day_of_week"],
            hour=conf["hour"],
            minute=conf["minute"],
            timezone=TIMEZONE,
        )
    return CronTrigger(
        hour=conf["hour"],
        minute=conf["minute"],
        timezone=TIMEZONE,
    )


def _apply_jobs() -> None:
    """把当前配置同步到调度器（增删改）。"""
    if _scheduler is None:
        return
    cfg = load_config()

    for job_key, conf in cfg.items():
        existing = _scheduler.get_job(job_key)
        if not conf.get("enabled"):
            if existing:
                _scheduler.remove_job(job_key)
                logger.info("[Scheduler] 已停用任务 %s", JOB_LABELS[job_key])
            continue

        trigger = _build_trigger(job_key, conf)
        _scheduler.add_job(
            _run_wrapped,
            trigger,
            args=[job_key],
            id=job_key,
            name=JOB_LABELS[job_key],
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )


def start_scheduler() -> None:
    """启动调度器并按配置装载任务。"""
    global _scheduler
    if _scheduler is not None:
        logger.warning("[Scheduler] 调度器已在运行，跳过重复启动")
        return

    _scheduler = BackgroundScheduler(timezone=TIMEZONE)
    _scheduler.start()
    _apply_jobs()
    logger.info("[Scheduler] 调度器已启动（时区 %s）", TIMEZONE)


def stop_scheduler() -> None:
    """停止调度器（应用关闭时调用）。"""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("[Scheduler] 调度器已停止")


def apply_config(new_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """保存新配置并立即重载任务。返回最新状态。"""
    clean = save_config(new_cfg)

    if _scheduler is None:
        # 调度器没跑，先把配置存好，启动时自动生效
        start_scheduler()
    else:
        _apply_jobs()

    logger.info("[Scheduler] 计划已更新: %s", clean)
    return get_scheduler_status()


def run_job_now(job_key: str) -> Dict[str, Any]:
    """立即在后台线程执行一次任务（不阻塞 HTTP 请求）。

    同一任务已在运行时直接拒绝，避免并发重复打赛狐接口。
    """
    if job_key not in _JOB_FUNCS:
        return {"ok": False, "message": f"未知任务：{job_key}"}

    if _is_running(job_key):
        return {
            "ok": False,
            "busy": True,
            "message": f"{JOB_LABELS[job_key]}正在执行中，请等待完成后再试",
        }

    thread = threading.Thread(
        target=_run_wrapped,
        args=(job_key,),
        name=f"manual-{job_key}",
        daemon=True,
    )
    thread.start()
    return {
        "ok": True,
        "started": True,
        "message": f"{JOB_LABELS[job_key]}已开始执行，请稍候查看结果",
    }


# ---------------------------------------------------------------------------
# 状态查询
# ---------------------------------------------------------------------------
def _describe_plan(job_key: str, conf: Dict[str, Any]) -> str:
    """把配置翻译成人话。"""
    if not conf.get("enabled"):
        return "已停用"
    hhmm = f"{conf['hour']:02d}:{conf['minute']:02d}"
    if job_key == JOB_PRODUCT:
        return f"每{DAY_LABELS.get(conf['day_of_week'], conf['day_of_week'])} {hhmm}"
    return f"每天 {hhmm}"


def get_scheduler_status() -> Dict[str, Any]:
    """返回调度器状态、任务计划与运行状态。"""
    cfg = load_config()

    jobs: List[Dict[str, Any]] = []
    for job_key in (JOB_PRODUCT, JOB_SEARCH):
        conf = cfg.get(job_key, {})
        job_obj = _scheduler.get_job(job_key) if _scheduler else None
        with _state_lock:
            state = dict(_JOB_STATE.get(job_key, {}))

        jobs.append({
            "id": job_key,
            "name": JOB_LABELS[job_key],
            "enabled": bool(conf.get("enabled")),
            "plan": _describe_plan(job_key, conf),
            "config": conf,
            "next_run_time": (
                str(job_obj.next_run_time) if job_obj and job_obj.next_run_time else None
            ),
            "running": bool(state.get("running")),
            "last_run": state.get("last_run"),
            "last_result": state.get("last_result"),
            "last_error": state.get("last_error"),
        })

    return {
        "running": bool(_scheduler and _scheduler.running),
        "timezone": TIMEZONE,
        "jobs": jobs,
        "config": cfg,
        "valid_days": [{"value": d, "label": DAY_LABELS[d]} for d in VALID_DAYS],
    }
