"""健康度分段重算引擎。

设计目标（对应分段重算改造）：
- 按「评估范围 × 证据类别」拆分计算段，每段独立保存状态、证据区间与错误信息；
- 已收口（completed）的证据段在重试/恢复时直接复用，只处理未完成部分；
- 每个评分任务绑定一个口径版本（spec_version）并保存证据快照，口径升级后新版本全量重算，
  旧报告继续使用原快照；
- 同一评估范围同一时刻只允许一个活跃评分任务，重复启动不会生成第二份评分；
- 全局并发上限 MAX_CONCURRENT_JOBS，两个任务并发写同一版本时以首个持有者为准；
- 三类健康分与综合评分必须来自同一证据版本，未收口的报告不能公示。

引擎只依赖内存 store（与项目其余部分一致），用线程锁保证并发写入安全。
"""
from __future__ import annotations

import math
import threading
from datetime import datetime
from typing import Any

from app.store import store

# ---------------------------------------------------------------------------
# 口径与分段定义
# ---------------------------------------------------------------------------

# 当前评分口径版本。口径（权重/公式/证据来源）调整时递增，新版本任务必须全量重算。
CURRENT_SPEC_VERSION = "v1"

# 每个评估范围最多保留的证据快照版本数（按开始时间淘汰最旧的）。
SNAPSHOT_KEEP = 3

# 全局同时运行的评分任务上限。
MAX_CONCURRENT_JOBS = 2

# 证据类别 -> （健康分字段, 来源模块, 来源证据字段, 分段中文名）
CATEGORY_SPEC: dict[str, dict[str, str]] = {
    "storage": {
        "label": "存储健康分",
        "score_field": "存储健康分",
        "module": "cost",
        "evidence_field": "冷数据占比",
        "ref_field": "报告编号",
    },
    "compute": {
        "label": "计算健康分",
        "score_field": "计算健康分",
        "module": "drift",
        "evidence_field": "偏差比例",
        "ref_field": "记录编号",
    },
    "quality": {
        "label": "质量健康分",
        "score_field": "质量健康分",
        "module": "dedup",
        "evidence_field": "影响行数",
        "ref_field": "任务编号",
    },
}

# 综合评分收口段（必须等三类证据段全部收口后才能运行）。
COMPOSITE_SEGMENT = "composite"

SEGMENT_ORDER = ["storage", "compute", "quality", COMPOSITE_SEGMENT]

# 分段状态
SEG_PENDING = "pending"      # 未开始
SEG_RUNNING = "running"      # 计算中
SEG_COMPLETED = "completed"  # 已收口（证据与分值已固化，重试时复用）
SEG_FAILED = "failed"        # 失败（保留错误说明，允许重试）

# 任务状态
JOB_EVALUATING = "评估中"
JOB_READY = "已生成"
JOB_PUBLISHED = "已公示"
JOB_ARCHIVED = "已归档"

# 综合评分权重（同属一个口径版本，写进快照）。
SCORE_WEIGHTS = {"storage": 0.4, "compute": 0.3, "quality": 0.3}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_number(value: Any) -> float | None:
    """把 '32%'、'0.32'、'1280' 这类证据值解析成数字；无法解析返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip().replace("％", "%")
    if not text:
        return None
    percent = text.endswith("%")
    text = text.rstrip("%").strip()
    try:
        number = float(text)
    except ValueError:
        return None
    if percent:
        number = number / 100.0
    return number


def _round1(value: float) -> float:
    return round(value + 1e-9, 1)


class ScoringEngine:
    """分段重算引擎：任务、分段、证据快照与并发控制都收口在这里。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._running_scopes: set[str] = set()
        self._semaphore = threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)

    # ------------------------------------------------------------------
    # 任务存取与历史兼容
    # ------------------------------------------------------------------
    def _table(self) -> list[dict[str, Any]]:
        return store.rows("scoring_job")

    def _find_job(self, job_id: int) -> dict[str, Any] | None:
        return store.find("scoring_job", job_id)

    def ensure_legacy_jobs(self) -> None:
        """把改造前已存在的健康度报告兼容成评分任务。

        历史报告没有分段/快照信息，标记为 legacy：不强制重算，保留原展示值；
        一旦对其执行「开始评估」，就按当前口径建立完整分段并进入正常流程。
        """
        with self._lock:
            table = self._table()
            known = {int(job["score_entry_id"]) for job in table}
            for report in store.rows(MODULE_SCORE):
                entry_id = int(report.get("id", 0))
                if entry_id in known:
                    continue
                table.append(self._blank_job(report, spec_version=CURRENT_SPEC_VERSION, legacy=True))

    def _blank_job(
        self,
        report: dict[str, Any],
        *,
        spec_version: str,
        legacy: bool,
    ) -> dict[str, Any]:
        segments: dict[str, Any] = {}
        for key in SEGMENT_ORDER:
            segments[key] = {
                "key": key,
                "name": "综合评分收口" if key == COMPOSITE_SEGMENT else CATEGORY_SPEC[key]["label"],
                "status": SEG_PENDING,
                "evidence_refs": [],
                "evidence_range": None,
                "score": None,
                "error": None,
                "updated_at": None,
            }
        return {
            "id": self._next_job_id(),
            "score_entry_id": int(report.get("id", 0)),
            "报告编号": report.get("报告编号"),
            "评估范围": report.get("评估范围"),
            "spec_version": spec_version,
            "legacy": legacy,
            "status": str(report.get("status") or "草稿"),
            "segments": segments,
            "snapshot": None,
            "failed_categories": [],
            "progress": {"completed": 0, "total": len(SEGMENT_ORDER), "percent": 0},
            "created_at": _now(),
            "updated_at": _now(),
            "running_token": None,
        }

    def _next_job_id(self) -> int:
        return max((int(job.get("id", 0)) for job in self._table()), default=0) + 1

    # ------------------------------------------------------------------
    # 启动：同范围互斥 + 全局并发上限 + 同版本首持有有效
    # ------------------------------------------------------------------
    def start_job(self, score_entry_id: int) -> tuple[dict[str, Any] | None, str]:
        report = store.find(MODULE_SCORE, score_entry_id)
        if report is None:
            return None, f"健康度报告 {score_entry_id} 不存在或已归档"

        with self._lock:
            self.ensure_legacy_jobs()
            scope = str(report.get("评估范围") or "").strip()
            if not scope:
                return None, "评估范围为空，无法拆分计算段"

            # 同范围互斥：已经有活跃任务（持有运行令牌）时拒绝，避免同一范围生成两份评分。
            # 只认 running_token：进程重启后遗留的「评估中」状态没有令牌，应允许恢复而不是误拦。
            for job in self._table():
                if str(job.get("评估范围")) != scope:
                    continue
                if job.get("running_token"):
                    return None, f"评估范围「{scope}」已有进行中的评分任务，不能重复启动"

            # 已公示/已归档报告使用原快照，不允许再启动重算（如需新口径请新建报告）。
            current = self._job_of_entry(score_entry_id)
            if current is not None and current.get("status") in (JOB_PUBLISHED, JOB_ARCHIVED):
                return None, "报告已公示，评分快照已锁定，不能重新评估"

            # 可复用任务：同报告、同口径、非 legacy 的进行中/已收口任务。
            # - 中断恢复：只跑未收口段；
            # - 已收口后再次启动：全部段已 completed，直接幂等返回，不重新扫描、不新建任务。
            reusable = self._recoverable_job(scope, CURRENT_SPEC_VERSION, score_entry_id)
            if reusable is not None:
                job = reusable
                if self._is_closed(job):
                    return job, "评分已按当前口径收口，复用既有证据快照"
                message = "恢复未完成的评分任务，仅重算未收口分段"
            else:
                job = self._blank_job(report, spec_version=CURRENT_SPEC_VERSION, legacy=False)
                self._table().append(job)
                message = "已按当前口径创建分段评分任务"

            if not self._semaphore.acquire(blocking=False):
                return None, f"全局评分并发已达上限（{MAX_CONCURRENT_JOBS}），请稍后重试"
            # 首个持有者写入 running_token；并发情况下后到者在上面的互斥判断已被拦下，
            # 这里再做一次 token 校验，保证同一版本只有首个写入有效。
            token = f"tok-{job['id']}-{int(datetime.now().timestamp() * 1000)}"
            if job.get("running_token"):
                self._semaphore.release()
                return None, "该评分任务已被其他流程持有，本次启动无效"
            job["running_token"] = token
            job["status"] = JOB_EVALUATING
            job["updated_at"] = _now()
            self._running_scopes.add(scope)
            self._sync_report(job)

        try:
            self._run_job(job["id"], token)
        finally:
            with self._lock:
                if job.get("running_token") == token:
                    job["running_token"] = None
                    self._running_scopes.discard(scope)
                self._semaphore.release()
        return self._find_job(job["id"]), message

    def _recoverable_job(
        self, scope: str, spec_version: str, score_entry_id: int
    ) -> dict[str, Any] | None:
        """同报告、同口径、非历史兼容的任务都可恢复（进行中或已收口均可幂等复用）。"""
        candidates = [
            job
            for job in self._table()
            if int(job.get("score_entry_id", 0)) == score_entry_id
            and str(job.get("评估范围")) == scope
            and str(job.get("spec_version")) == spec_version
            and not job.get("legacy")
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: int(item.get("id", 0)))

    def _retire_old_snapshots(self, scope: str) -> None:
        """同范围只保留最近 SNAPSHOT_KEEP 个快照版本；超出的标记淘汰但不删除数据，
        保证旧报告仍能读取原快照（分值与证据引用不变）。"""
        snapshotted = [
            job
            for job in self._table()
            if str(job.get("评估范围")) == scope and job.get("snapshot")
        ]
        snapshotted.sort(key=lambda item: int(item.get("id", 0)), reverse=True)
        for stale in snapshotted[SNAPSHOT_KEEP:]:
            stale["snapshot_retired"] = True

    # ------------------------------------------------------------------
    # 分段执行
    # ------------------------------------------------------------------
    def _run_job(self, job_id: int, token: str) -> None:
        job = self._find_job(job_id)
        if job is None:
            return
        for key in SEGMENT_ORDER:
            with self._lock:
                if job.get("running_token") != token:
                    return  # 持有者已失效，停止后续写入
                segment = job["segments"][key]
                if segment["status"] == SEG_COMPLETED:
                    continue  # 已收口证据段直接复用，不重复扫描
                if key == COMPOSITE_SEGMENT:
                    self._run_composite(job, segment, token)
                else:
                    self._run_evidence_segment(job, segment, key, token)
                self._refresh_progress(job)
                self._sync_report(job)

    def _run_evidence_segment(
        self,
        job: dict[str, Any],
        segment: dict[str, Any],
        category: str,
        token: str,
    ) -> None:
        spec = CATEGORY_SPEC[category]
        segment["status"] = SEG_RUNNING
        segment["updated_at"] = _now()
        try:
            evidence = self._collect_evidence(job["评估范围"], spec)
            if not evidence:
                raise ValueError(
                    f"{spec['label']}暂无可用证据：模块「{spec['module']}」在评估范围"
                    f"「{job['评估范围']}」内没有可解析的{spec['evidence_field']}"
                )
            score = self._score_category(category, evidence)
            segment["status"] = SEG_COMPLETED
            segment["score"] = score
            segment["error"] = None
            segment["evidence_refs"] = [item["ref"] for item in evidence]
            segment["evidence_range"] = {
                "module": spec["module"],
                "field": spec["evidence_field"],
                "start_ref": evidence[0]["ref"],
                "end_ref": evidence[-1]["ref"],
                "count": len(evidence),
                "spec_version": job["spec_version"],
            }
            segment["updated_at"] = _now()
            # 状态结果回写到来源清单（成本报告/漂移/去重），来源行直接可见被哪份评分引用。
            self._write_back_to_sources(job, category, spec, evidence)
        except Exception as exc:  # 单段失败不影响其它段，记录原因等待重试
            segment["status"] = SEG_FAILED
            segment["error"] = str(exc)
            segment["updated_at"] = _now()

    def _write_back_to_sources(
        self,
        job: dict[str, Any],
        category: str,
        spec: dict[str, str],
        evidence: list[dict[str, Any]],
    ) -> None:
        """把分段收口结果回写到来源业务行的「评分回写」字段。

        同一任务重算时按 job_id 覆盖旧条目，不会产生重复回写；
        不同评分任务引用同一证据时各自保留一条，来源侧能看到全部复用方。
        """
        entry = {
            "job_id": job["id"],
            "报告编号": job["报告编号"],
            "评估范围": job["评估范围"],
            "category": category,
            "category_label": spec["label"],
            "category_score": job["segments"][category]["score"],
            "spec_version": job["spec_version"],
            "evidence_field": spec["evidence_field"],
            "evidence_range": job["segments"][category]["evidence_range"],
            "回写时间": _now(),
        }
        referenced_ids = {item["row_id"] for item in evidence}
        for row in store.rows(spec["module"]):
            if int(row.get("id", 0)) not in referenced_ids:
                continue
            writebacks = [w for w in row.get("评分回写", []) if w.get("job_id") != job["id"]]
            writebacks.append(entry)
            row["评分回写"] = writebacks
            row["评分回写摘要"] = self._writeback_summary(writebacks)

    def _collect_evidence(self, scope: str, spec: dict[str, str]) -> list[dict[str, Any]]:
        """扫描来源模块，固化评估范围内的证据（含引用编号与原始值）。

        成本报告自带「分析范围」，按评估范围精确匹配实现分段；
        漂移/去重为全域检测记录，评估范围内全部纳入，证据区间标注所属范围。
        """
        evidence: list[dict[str, Any]] = []
        scope_field = "分析范围" if spec["module"] == "cost" else None
        for row in store.rows(spec["module"]):
            if scope_field and str(row.get(scope_field) or "").strip() != scope:
                continue
            raw_value = row.get(spec["evidence_field"])
            number = _parse_number(raw_value)
            if number is None:
                continue  # 空态/非数值证据不计入，留待该段以「暂无证据」失败
            evidence.append({
                "ref": str(row.get(spec["ref_field"], "")),
                "module": spec["module"],
                "field": spec["evidence_field"],
                "raw": raw_value,
                "value": number,
                "row_id": int(row.get("id", 0)),
                "scope": scope,
            })
        return evidence

    def _score_category(self, category: str, evidence: list[dict[str, Any]]) -> float:
        values = [item["value"] for item in evidence]
        if category == "storage":
            # 冷数据占比越低越健康：100 - 平均冷数据占比（按百分数计）。
            avg = sum(values) / len(values)
            ratio = avg * 100 if avg <= 1 else avg
            return _round1(max(0.0, min(100.0, 100.0 - ratio)))
        if category == "compute":
            # 漂移偏差比例绝对值越小越健康。
            avg = sum(abs(v) for v in values) / len(values)
            ratio = avg * 100 if avg <= 1 else avg
            return _round1(max(0.0, min(100.0, 100.0 - ratio)))
        # quality：去重影响行数做对数压缩，影响越大扣分越多。
        avg_rows = max(0.0, sum(values) / len(values))
        penalty = 100.0 * (1.0 - math.exp(-avg_rows / 10000.0))
        return _round1(max(0.0, min(100.0, 100.0 - penalty)))

    def _run_composite(
        self, job: dict[str, Any], segment: dict[str, Any], token: str
    ) -> None:
        segment["status"] = SEG_RUNNING
        segment["updated_at"] = _now()
        failed = [
            key
            for key in ("storage", "compute", "quality")
            if job["segments"][key]["status"] != SEG_COMPLETED
        ]
        if failed:
            segment["status"] = SEG_FAILED
            names = "、".join(CATEGORY_SPEC[key]["label"] for key in failed)
            segment["error"] = f"综合评分收口缺少已收口证据类别：{names}"
            segment["updated_at"] = _now()
            return
        scores = {key: float(job["segments"][key]["score"]) for key in SCORE_WEIGHTS}
        composite = _round1(sum(scores[key] * weight for key, weight in SCORE_WEIGHTS.items()))
        segment["status"] = SEG_COMPLETED
        segment["score"] = composite
        segment["error"] = None
        segment["updated_at"] = _now()
        # 全部收口：固化同一证据版本的快照，报告才允许进入已生成/公示。
        job["snapshot"] = {
            "spec_version": job["spec_version"],
            "scores": {key: job["segments"][key]["score"] for key in SCORE_WEIGHTS},
            "composite": composite,
            "weights": dict(SCORE_WEIGHTS),
            "evidence_ranges": {
                key: job["segments"][key]["evidence_range"] for key in SCORE_WEIGHTS
            },
            "evidence_refs": {
                key: list(job["segments"][key]["evidence_refs"]) for key in SCORE_WEIGHTS
            },
            "frozen_at": _now(),
        }
        job["status"] = JOB_READY
        # 综合评分回写到三类来源清单，让成本/漂移/去重行都能看到所属报告的综合分与状态。
        self._write_composite_to_sources(job, composite)
        # 快照在此刻才真正生成，收口后按范围执行旧版本淘汰（保留最近 SNAPSHOT_KEEP 个）。
        self._retire_old_snapshots(str(job["评估范围"]))

    def _write_composite_to_sources(self, job: dict[str, Any], composite: float) -> None:
        for category, spec in CATEGORY_SPEC.items():
            refs = set(job["segments"][category].get("evidence_refs") or [])
            if not refs:
                continue
            for row in store.rows(spec["module"]):
                if str(row.get(spec["ref_field"], "")) not in refs:
                    continue
                for writeback in row.get("评分回写", []):
                    if writeback.get("job_id") == job["id"]:
                        writeback["综合评分"] = composite
                        writeback["报告状态"] = JOB_READY
                row["评分回写摘要"] = self._writeback_summary(row.get("评分回写", []))

    @staticmethod
    def _writeback_summary(writebacks: list[dict[str, Any]]) -> str:
        if not writebacks:
            return ""
        latest = writebacks[-1]
        composite = latest.get("综合评分")
        composite_text = f"，综合 {composite}" if composite is not None else ""
        return (
            f"{latest['报告编号']}（{latest['spec_version']}）引用 → "
            f"{latest['category_label']} {latest['category_score']}{composite_text}"
        )

    # ------------------------------------------------------------------
    # 重试：只处理未完成（失败/未开始）分段，已收口分段复用
    # ------------------------------------------------------------------
    def retry_segment(self, score_entry_id: int, segment_key: str) -> tuple[dict[str, Any] | None, str]:
        report = store.find(MODULE_SCORE, score_entry_id)
        if report is None:
            return None, f"健康度报告 {score_entry_id} 不存在或已归档"
        with self._lock:
            self.ensure_legacy_jobs()
            job = self._job_of_entry(score_entry_id)
            if job is None:
                return None, "评分任务不存在"
            if segment_key not in job["segments"]:
                return None, f"计算段「{segment_key}」不存在"
            if job.get("status") in (JOB_PUBLISHED, JOB_ARCHIVED):
                return None, "报告已公示，快照已锁定，不能重试"
            if not self._semaphore.acquire(blocking=False):
                return None, f"全局评分并发已达上限（{MAX_CONCURRENT_JOBS}），请稍后重试"
            scope = str(job["评估范围"])
            if scope in self._running_scopes:
                self._semaphore.release()
                return None, f"评估范围「{scope}」已有任务在运行，请等待其收口"
            token = f"tok-{job['id']}-retry-{int(datetime.now().timestamp() * 1000)}"
            job["running_token"] = token
            job["status"] = JOB_EVALUATING
            self._running_scopes.add(scope)
        try:
            with self._lock:
                segment = job["segments"][segment_key]
                if segment["status"] == SEG_COMPLETED:
                    return job, "该计算段已收口，复用既有证据与分值"
                if segment_key == COMPOSITE_SEGMENT:
                    self._run_composite(job, segment, token)
                else:
                    self._run_evidence_segment(job, segment, segment_key, token)
                # 证据段补齐后自动尝试综合收口。
                if segment_key != COMPOSITE_SEGMENT:
                    comp = job["segments"][COMPOSITE_SEGMENT]
                    if comp["status"] != SEG_COMPLETED:
                        self._run_composite(job, comp, token)
                self._refresh_progress(job)
                self._sync_report(job)
        finally:
            with self._lock:
                if job.get("running_token") == token:
                    job["running_token"] = None
                    self._running_scopes.discard(scope)
                self._semaphore.release()
        return self._find_job(job["id"]), f"计算段「{segment_key}」已重试"

    def retry_all_failed(self, score_entry_id: int) -> tuple[dict[str, Any] | None, str]:
        with self._lock:
            self.ensure_legacy_jobs()
            job = self._job_of_entry(score_entry_id)
            if job is None:
                return None, "评分任务不存在"
            failed_keys = [
                key for key in SEGMENT_ORDER
                if job["segments"][key]["status"] in (SEG_FAILED, SEG_PENDING)
            ]
        if not failed_keys:
            return job, "没有待重试的计算段"
        last: dict[str, Any] | None = None
        for key in failed_keys:
            last, _ = self.retry_segment(score_entry_id, key)
        return last, f"已重试 {len(failed_keys)} 个未收口计算段"

    # ------------------------------------------------------------------
    # 口径升级：新版本必须重算，旧报告保留原快照
    # ------------------------------------------------------------------
    def bump_spec(self, new_version: str | None = None) -> str:
        global CURRENT_SPEC_VERSION
        with self._lock:
            if new_version:
                CURRENT_SPEC_VERSION = new_version
            else:
                # 形如 v1 -> v2：拆出数字部分递增，保持单 v 前缀。
                digits = "".join(ch for ch in CURRENT_SPEC_VERSION if ch.isdigit())
                prefix = CURRENT_SPEC_VERSION[: len(CURRENT_SPEC_VERSION) - len(digits)] or "v"
                CURRENT_SPEC_VERSION = f"{prefix}{(int(digits) if digits else 0) + 1}"
            return CURRENT_SPEC_VERSION

    def current_spec(self) -> str:
        """运行时口径版本：始终读取模块全局值，避免调用方按值导入拿到陈旧版本。"""
        return CURRENT_SPEC_VERSION

    # ------------------------------------------------------------------
    # 公示控制：未收口不能公示；三类分与综合分必须同版本
    # ------------------------------------------------------------------
    def publish(self, score_entry_id: int) -> tuple[dict[str, Any] | None, str]:
        with self._lock:
            self.ensure_legacy_jobs()
            job = self._job_of_entry(score_entry_id)
            if job is None:
                return None, "评分任务不存在"
            if job.get("legacy") and not job.get("snapshot"):
                return None, "历史报告缺少统一证据快照，请先完成分段评估再公示"
            if not self._is_closed(job):
                missing = self._missing_categories(job)
                return None, f"报告尚未收口，缺失类别：{'、'.join(missing) or '综合评分'}，暂不能公示"
            versions = {
                job["segments"][key].get("evidence_range", {}).get("spec_version")
                for key in SCORE_WEIGHTS
            }
            if versions != {job["spec_version"]}:
                return None, "三类健康分与综合评分来自不同证据版本，不能公示"
            job["status"] = JOB_PUBLISHED
            job["updated_at"] = _now()
            self._sync_report(job)
            return job, "健康度报告已公示"

    def archive(self, score_entry_id: int) -> tuple[dict[str, Any] | None, str]:
        with self._lock:
            self.ensure_legacy_jobs()
            job = self._job_of_entry(score_entry_id)
            if job is None:
                return None, "评分任务不存在"
            job["status"] = JOB_ARCHIVED
            job["updated_at"] = _now()
            self._sync_report(job)
            return job, "健康度报告已归档"

    # ------------------------------------------------------------------
    # 进度、状态回写与证据复用查询
    # ------------------------------------------------------------------
    def _job_of_entry(self, score_entry_id: int) -> dict[str, Any] | None:
        """返回该报告最新的评分任务（同报告可能存在多个口径版本）。"""
        matches = [
            job for job in self._table()
            if int(job.get("score_entry_id", 0)) == score_entry_id
        ]
        if not matches:
            return None
        return max(matches, key=lambda item: int(item.get("id", 0)))

    def _is_closed(self, job: dict[str, Any]) -> bool:
        return all(job["segments"][key]["status"] == SEG_COMPLETED for key in SEGMENT_ORDER)

    def _missing_categories(self, job: dict[str, Any]) -> list[str]:
        return [
            CATEGORY_SPEC[key]["label"]
            for key in ("storage", "compute", "quality")
            if job["segments"][key]["status"] != SEG_COMPLETED
        ]

    def _refresh_progress(self, job: dict[str, Any]) -> None:
        completed = sum(
            1 for key in SEGMENT_ORDER if job["segments"][key]["status"] == SEG_COMPLETED
        )
        total = len(SEGMENT_ORDER)
        job["progress"] = {
            "completed": completed,
            "total": total,
            "percent": round(completed * 100 / total),
        }
        job["failed_categories"] = [
            key for key in SEGMENT_ORDER if job["segments"][key]["status"] == SEG_FAILED
        ]
        if not self._is_closed(job) and job["status"] != JOB_EVALUATING:
            job["status"] = JOB_EVALUATING
        job["updated_at"] = _now()

    def _sync_report(self, job: dict[str, Any]) -> None:
        """把分段状态/进度/快照分回写到成本报告清单同一行的健康度报告记录。"""
        report = store.find(MODULE_SCORE, int(job["score_entry_id"]))
        if report is None:
            return
        report["status"] = job["status"]
        report["pending"] = job["status"] != JOB_ARCHIVED
        report["abnormal"] = bool(job.get("failed_categories"))
        report["spec_version"] = job["spec_version"]
        report["progress"] = dict(job["progress"])
        report["failed_categories"] = list(job.get("failed_categories", []))
        report["segments"] = {
            key: {
                "status": job["segments"][key]["status"],
                "score": job["segments"][key]["score"],
                "error": job["segments"][key]["error"],
                "evidence_range": job["segments"][key]["evidence_range"],
            }
            for key in SEGMENT_ORDER
        }
        if job.get("snapshot"):
            snapshot = job["snapshot"]
            for key, spec in CATEGORY_SPEC.items():
                report[spec["score_field"]] = snapshot["scores"][key]
            report["综合评分"] = snapshot["composite"]
            report["evidence_version"] = snapshot["spec_version"]
            report["evidence_refs"] = snapshot["evidence_refs"]
            report["评估时间"] = snapshot["frozen_at"]
        else:
            # 未收口时显式置空，前端据此显示「暂无证据版本」而不是字段缺失。
            report["evidence_version"] = None
            if job.get("legacy"):
                report["evidence_refs"] = None

    def get_job(self, score_entry_id: int) -> dict[str, Any] | None:
        with self._lock:
            self.ensure_legacy_jobs()
            return self._job_of_entry(score_entry_id)

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            self.ensure_legacy_jobs()
            return list(self._table())

    def evidence_usage(self, module: str, row_id: int) -> list[dict[str, Any]]:
        """另一侧业务入口查询：某条证据被哪些评分任务、哪段证据区间复用。"""
        with self._lock:
            self.ensure_legacy_jobs()
            source = store.find(module, row_id)
            if source is None:
                return []
            usages: list[dict[str, Any]] = []
            for job in self._table():
                for key, spec in CATEGORY_SPEC.items():
                    if spec["module"] != module:
                        continue
                    segment = job["segments"][key]
                    refs = segment.get("evidence_refs") or []
                    ref_value = str(source.get(spec["ref_field"], ""))
                    if ref_value and ref_value in refs:
                        usages.append({
                            "job_id": job["id"],
                            "报告编号": job["报告编号"],
                            "评估范围": job["评估范围"],
                            "category": key,
                            "category_label": spec["label"],
                            "spec_version": job["spec_version"],
                            "evidence_range": segment.get("evidence_range"),
                            "segment_status": segment["status"],
                        })
            return usages


MODULE_SCORE = "score"
engine = ScoringEngine()
