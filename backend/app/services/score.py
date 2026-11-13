"""数仓健康度评估业务规则：状态流转、分段重算与证据快照都收在这里。

评分数据流：
    成本报告(cost.冷数据占比) ─┐
    漂移记录(drift.偏差比例) ─┼─> 分段重算引擎 ─> 三类健康分 + 综合评分（同一证据版本）
    去重任务(dedup.影响行数) ─┘
动作语义：
- 开始评估：启动/恢复分段评分任务（同范围互斥、同版本只跑未完成段）；
- 重试失败段：只重算失败/未收口的计算段，已收口证据直接复用；
- 公示报告：仅当全部计算段收口、三类分与综合分同版本时才允许；
- 归档报告：锁定快照。
"""
from __future__ import annotations

from typing import Any

from app.services.scoring_engine import (
    JOB_ARCHIVED,
    JOB_PUBLISHED,
    engine,
)
from app.store import store

MODULE = "score"
REQUIRED_FIELDS = ["报告编号", "评估范围", "存储健康分"]
STATUS_ORDER = ["草稿", "评估中", "已生成", "已公示"]
# 兼容旧前端的动作名 -> 内部语义
ACTION_RULES = {"开始评估": "start", "公示报告": "publish", "归档报告": "archive"}
NEGATIVE_ACTIONS = []


class ScoreService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        engine.ensure_legacy_jobs()
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("报告编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        engine.ensure_legacy_jobs()
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        # 新建报告初始为空态：还没有任何证据版本，分值留空而不是写占位文案。
        entry.update({
            "计算健康分": None,
            "质量健康分": None,
            "综合评分": None,
            "改进建议": values.get("改进建议"),
            "评估时间": None,
            "spec_version": None,
            "evidence_version": None,
            "progress": {"completed": 0, "total": 4, "percent": 0},
            "failed_categories": [],
            "segments": {},
        })
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        engine.ensure_legacy_jobs()
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"健康度报告 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数仓健康度评估可执行范围"
        semantic = ACTION_RULES[action]
        if semantic == "start":
            job, message = engine.start_job(entry_id)
            if job is None:
                return None, message
            return store.find(MODULE, entry_id), message
        if semantic == "publish":
            job, message = engine.publish(entry_id)
            if job is None:
                return None, message
            return store.find(MODULE, entry_id), message
        # archive
        job, message = engine.archive(entry_id)
        if job is None:
            return None, message
        return store.find(MODULE, entry_id), message

    # -- 分段重算专属能力 -------------------------------------------------
    def get_job(self, entry_id: int) -> dict[str, Any] | None:
        return engine.get_job(entry_id)

    def retry_segment(self, entry_id: int, segment_key: str) -> tuple[dict[str, Any] | None, str]:
        job, message = engine.retry_segment(entry_id, segment_key)
        if job is None:
            return None, message
        return store.find(MODULE, entry_id), message

    def retry_all(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        job, message = engine.retry_all_failed(entry_id)
        if job is None:
            return None, message
        return store.find(MODULE, entry_id), message
