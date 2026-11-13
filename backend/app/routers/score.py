"""数仓健康度评估接口：健康度报告 + 分段重算任务、证据快照与口径版本。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.score import ScoreService
from app.services.scoring_engine import (
    MAX_CONCURRENT_JOBS,
    SEGMENT_ORDER,
    engine,
)

router = APIRouter(prefix="/api/score", tags=["数仓健康度评估"])

service = ScoreService()

LIST_FIELDS = ["报告编号", "评估范围", "存储健康分", "计算健康分", "质量健康分", "综合评分", "改进建议", "评估时间"]
STATUSES = ["草稿", "评估中", "已生成", "已公示"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按报告编号检索"),
    status: str | None = Query(default=None, description="草稿、评估中、已生成、已公示"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按报告编号与状态过滤数仓健康度评估列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数仓健康度评估清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "score", "total": total, "items": items}


@router.get("/spec")
def get_spec() -> dict[str, Any]:
    """返回当前评分口径版本与分段定义，前端据此渲染计算段。"""
    return {
        "current_spec_version": engine.current_spec(),
        "segments": SEGMENT_ORDER,
        "max_concurrent_jobs": MAX_CONCURRENT_JOBS,
        "snapshot_keep": 3,
    }


@router.post("/spec/bump", response_model=ActionResult)
def bump_spec(payload: EntryPayload) -> ActionResult:
    """升级评分口径（演示/管理用）：之后启动的评分任务按新版本全量重算。"""
    version = str(payload.values.get("spec_version") or "").strip() or None
    new_version = engine.bump_spec(version)
    return ActionResult(ok=True, message=f"评分口径已升级到 {new_version}，后续任务将全量重算", entry={"spec_version": new_version})


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条健康度报告，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="健康度报告已登记，尚未生成任何证据版本", entry=entry)


@router.get("/{entry_id}/job")
def get_job(entry_id: int) -> dict[str, Any]:
    """查看分段评分任务详情：四个计算段的状态、证据区间、错误与整体进度。"""
    job = service.get_job(entry_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"健康度报告 {entry_id} 不存在或尚未建立评分任务")
    return job


@router.post("/{entry_id}/retry", response_model=ActionResult)
def retry_all(entry_id: int) -> ActionResult:
    """整体重试：只重算失败/未收口的计算段，已收口证据段保持复用。"""
    entry, message = service.retry_all(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/segments/{segment_key}/retry", response_model=ActionResult)
def retry_segment(entry_id: int, segment_key: str) -> ActionResult:
    """单段重试：仅重算指定计算段（存储/计算/质量/综合收口）。"""
    if segment_key not in SEGMENT_ORDER:
        return ActionResult(ok=False, message=f"计算段「{segment_key}」不存在，可选：{'、'.join(SEGMENT_ORDER)}")
    entry, message = service.retry_segment(entry_id, segment_key)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条健康度报告明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"健康度报告 {entry_id} 不存在或已归档")
    return entry


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条健康度报告执行开始评估、公示报告、归档报告；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
