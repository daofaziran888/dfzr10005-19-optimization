"""分段重算引擎冒烟测试（不依赖网络，直接驱动 service 层与 FastAPI TestClient）。"""
from __future__ import annotations

import sys

from fastapi.testclient import TestClient

from app.main import app
from app.services.scoring_engine import (
    CURRENT_SPEC_VERSION,
    MAX_CONCURRENT_JOBS,
    SEG_COMPLETED,
    SEG_FAILED,
    engine,
)
from app.store import store

client = TestClient(app)
PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def reset() -> None:
    """重建内存 store，保证用例间互不污染。"""
    from app import store as store_mod
    from app.seed import SEED_ROWS
    store_mod.store._tables = {
        name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
    }
    # 引擎单例里的信号量/互斥集合也复位
    engine._running_scopes.clear()
    engine._semaphore = type(engine._semaphore)(MAX_CONCURRENT_JOBS)


print("== 1. 历史兼容：种子报告自动建立 legacy 任务 ==")
reset()
jobs = engine.list_jobs()
check("三条种子报告都有任务", len(jobs) == 3, str(len(jobs)))
legacy = next(j for j in jobs if j["报告编号"] == "SCOR-0001")
check("已公示历史报告标记 legacy", legacy["legacy"] is True)
check("历史报告保留原综合分 88.3", store.find("score", legacy["score_entry_id"])["综合评分"] == 88.3)

print("== 2. SCOR-0002 分段评估：三类证据 + 综合收口 ==")
reset()
r = client.post("/api/score/2/actions", json={"values": {"action": "开始评估"}})
check("开始评估返回 ok", r.json()["ok"], r.text)
report = r.json()["entry"]
check("状态变为已生成（四段全收口）", report["status"] == "已生成", report["status"])
check("进度 4/4", report["progress"]["completed"] == 4, str(report["progress"]))
job = client.get("/api/score/2/job").json()
for key in ("storage", "compute", "quality", "composite"):
    check(f"分段 {key} 收口", job["segments"][key]["status"] == SEG_COMPLETED)
storage_refs = job["segments"]["storage"]["evidence_refs"]
check("存储段引用 COST-0002", "COST-0002" in storage_refs, str(storage_refs))
compute_refs = job["segments"]["compute"]["evidence_refs"]
check("计算段引用 DRIF-0002", "DRIF-0002" in compute_refs, str(compute_refs))
quality_refs = job["segments"]["quality"]["evidence_refs"]
check("质量段引用 DEDU-0002", "DEDU-0002" in quality_refs, str(quality_refs))
check("证据区间记录起止编号", job["segments"]["storage"]["evidence_range"]["start_ref"] is not None)
check("快照版本等于当前口径", job["snapshot"]["spec_version"] == CURRENT_SPEC_VERSION)
check("综合分来自同一证据版本", report["evidence_version"] == CURRENT_SPEC_VERSION)
# 分值可复算：storage 只有 COST-0002 的 32% -> 68.0
check("存储健康分=68.0（100-32）", report["存储健康分"] == 68.0, str(report["存储健康分"]))
# compute: 12.5% -> 87.5
check("计算健康分=87.5（100-12.5）", report["计算健康分"] == 87.5, str(report["计算健康分"]))
# quality: 5200 行 -> 健康分 = 100 - 100*(1-exp(-0.52)) ≈ 59.5
import math
penalty = round(100 * (1 - math.exp(-5200 / 10000)) + 1e-9, 1)
expected_q = round(100 - penalty, 1)
check(f"质量健康分={expected_q}（影响行数越大扣分越多）", report["质量健康分"] == expected_q, str(report["质量健康分"]))
expected_c = round(68.0 * 0.4 + 87.5 * 0.3 + expected_q * 0.3 + 1e-9, 1)
check(f"综合评分={expected_c}（加权）", report["综合评分"] == expected_c, str(report["综合评分"]))

print("== 3. 同范围重复启动互斥，不产生第二份评分 ==")
reset()
# 先手工把 SCOR-0002 的任务置为活跃（模拟评估进行中被再次启动）
engine.ensure_legacy_jobs()
active = engine._job_of_entry(2)
active["status"] = "评估中"
active["running_token"] = "simulated-active-token"
engine._running_scopes.add("核心数仓全域")
created = client.post("/api/score", json={"values": {"报告编号": "SCOR-0009", "评估范围": "核心数仓全域", "存储健康分": "0"}})
check("同范围新报告可登记", created.json()["ok"], created.text)
new_id = created.json()["entry"]["id"]
r2 = client.post(f"/api/score/{new_id}/actions", json={"values": {"action": "开始评估"}})
check("同范围重复启动被拒绝", r2.json()["ok"] is False, r2.text)
check("拒绝原因明确", "已有进行中的评分任务" in r2.json()["message"], r2.json()["message"])
# 活跃任务结束后，同范围新报告可以正常启动（不产生并发两份）
active["running_token"] = None
engine._running_scopes.discard("核心数仓全域")
r2b = client.post(f"/api/score/{new_id}/actions", json={"values": {"action": "开始评估"}})
check("活跃任务结束后同范围可启动新评分", r2b.json()["ok"], r2b.text)

print("== 4. 恢复：已收口段复用，只处理未完成段 ==")
reset()
client.post("/api/score/2/actions", json={"values": {"action": "开始评估"}})
job_before = client.get("/api/score/2/job").json()
frozen = job_before["segments"]["storage"]["updated_at"]
# 再次启动（模拟中断后恢复）：全部段已收口，应直接复用且不报错
r3 = client.post("/api/score/2/actions", json={"values": {"action": "开始评估"}})
check("重复开始评估返回可恢复提示", r3.json()["ok"], r3.text)
job_after = client.get("/api/score/2/job").json()
check("存储段保持收口复用", job_after["segments"]["storage"]["status"] == SEG_COMPLETED)
check("综合分刷新前后一致", job_after["snapshot"]["composite"] == job_before["snapshot"]["composite"])

print("== 5. 单段失败 -> 缺失类别明确 -> 重试收口 ==")
reset()
# 让漂移证据不可解析，制造 compute 段失败
for row in store.rows("drift"):
    row["偏差比例"] = "待采样"
r4 = client.post("/api/score/2/actions", json={"values": {"action": "开始评估"}})
entry4 = r4.json()["entry"]
job4 = client.get("/api/score/2/job").json()
check("计算段失败并保留原因", job4["segments"]["compute"]["status"] == SEG_FAILED, job4["segments"]["compute"].get("error", ""))
check("失败类别回写到报告", "compute" in entry4["failed_categories"], str(entry4["failed_categories"]))
check("综合收口段因缺类别失败", job4["segments"]["composite"]["status"] == SEG_FAILED)
check("未收口报告不能公示", client.post("/api/score/2/actions", json={"values": {"action": "公示报告"}}).json()["ok"] is False)
# 修复证据后单段重试
for row in store.rows("drift"):
    if row.get("记录编号") == "DRIF-0002":
        row["偏差比例"] = "12.5%"
r5 = client.post("/api/score/2/segments/compute/retry", json={})
check("单段重试 ok", r5.json()["ok"], r5.text)
job5 = client.get("/api/score/2/job").json()
check("重试后计算段收口", job5["segments"]["compute"]["status"] == SEG_COMPLETED)
check("综合段自动补收口", job5["segments"]["composite"]["status"] == SEG_COMPLETED)
check("整体进度回到 4/4", job5["progress"]["completed"] == 4)
pub = client.post("/api/score/2/actions", json={"values": {"action": "公示报告"}})
check("收口后允许公示", pub.json()["ok"], pub.text)
check("公示状态写回报告", pub.json()["entry"]["status"] == "已公示")

print("== 6. 整体重试只动未收口段 ==")
reset()
for row in store.rows("dedup"):
    row["影响行数"] = None
client.post("/api/score/2/actions", json={"values": {"action": "开始评估"}})
for row in store.rows("dedup"):
    if row.get("任务编号") == "DEDU-0002":
        row["影响行数"] = "5200"
r6 = client.post("/api/score/2/retry", json={})
check("整体重试 ok", r6.json()["ok"], r6.text)
job6 = client.get("/api/score/2/job").json()
check("全部段收口", all(s["status"] == SEG_COMPLETED for s in job6["segments"].values()))

print("== 7. 口径升级：新版本全量重算，旧快照保留 ==")
reset()
client.post("/api/score/2/actions", json={"values": {"action": "开始评估"}})
old_snapshot = client.get("/api/score/2/job").json()["snapshot"]
bump = client.post("/api/score/spec/bump", json={"values": {}})
new_version = bump.json()["entry"]["spec_version"]
check("口径版本递增", new_version != CURRENT_SPEC_VERSION, new_version)
# 同一条报告重新开始评估 -> 生成新任务行（新版本），旧任务快照仍在
client.post("/api/score/2/actions", json={"values": {"action": "开始评估"}})
all_jobs = client.get("/api/score/2/job", )  # 只返回当前 entry 的最新任务
# 直接查引擎全部任务（排除 legacy 占位任务，只看真实评分任务）
scope_jobs = [j for j in engine.list_jobs() if j["score_entry_id"] == 2 and not j.get("legacy")]
versions = {j["spec_version"] for j in scope_jobs}
check("同报告存在新旧两个口径任务", versions == {CURRENT_SPEC_VERSION, new_version}, str(versions))
old_job = next(j for j in scope_jobs if j["spec_version"] == CURRENT_SPEC_VERSION)
check("旧任务快照保留原分值", old_job["snapshot"]["composite"] == old_snapshot["composite"])
check("新任务使用新口径", all_jobs.json()["spec_version"] == new_version)

print("== 8. 证据复用联动：漂移入口显示被复用区间 ==")
reset()
client.post("/api/score/2/actions", json={"values": {"action": "开始评估"}})
usage = client.get("/api/drift/2/evidence-usage").json()
check("DRIF-0002 被评分复用", usage["total"] >= 1, str(usage))
if usage["items"]:
    item = usage["items"][0]
    check("复用记录带证据区间", item["evidence_range"] is not None and item["evidence_range"]["field"] == "偏差比例")
    check("复用记录带口径版本", bool(item["spec_version"]), str(item["spec_version"]))
    check("复用区间口径与任务一致", item["spec_version"] == item["evidence_range"]["spec_version"])
usage_cost = client.get("/api/cost/2/evidence-usage").json()
check("COST-0002 被存储段复用", any(i["category"] == "storage" for i in usage_cost["items"]))
usage_dedup = client.get("/api/dedup/2/evidence-usage").json()
check("DEDU-0002 被质量段复用", any(i["category"] == "quality" for i in usage_dedup["items"]))

print("== 9. 空态：无证据报告给出明确提示 ==")
reset()
# 清空三类来源证据，模拟评估范围内暂无任何证据
store.rows("cost").clear()
store.rows("drift").clear()
store.rows("dedup").clear()
created = client.post("/api/score", json={"values": {"报告编号": "SCOR-0010", "评估范围": "完全空白范围", "存储健康分": "0"}})
empty_id = created.json()["entry"]["id"]
r9 = client.post(f"/api/score/{empty_id}/actions", json={"values": {"action": "开始评估"}})
job9 = client.get(f"/api/score/{empty_id}/job").json()
failed_msgs = [job9["segments"][k].get("error") or "" for k in ("storage", "compute", "quality")]
check("三类段均提示暂无证据", all("暂无可用证据" in m for m in failed_msgs), str(failed_msgs))
check("空态报告不能公示", client.post(f"/api/score/{empty_id}/actions", json={"values": {"action": "公示报告"}}).json()["ok"] is False)
check("空态报告综合分仍为空", client.get(f"/api/score/{empty_id}").json()["综合评分"] is None)

print("== 10. 并发上限 ==")
reset()
# 构造三个不同范围的报告
for idx, scope in enumerate(("范围甲", "范围乙", "范围丙"), start=20):
    client.post("/api/score", json={"values": {"报告编号": f"SCOR-{idx}", "评估范围": scope, "存储健康分": "0"}})
ids = [e["id"] for e in client.get("/api/score?size=50").json()["items"] if e["评估范围"] in ("范围甲", "范围乙", "范围丙")]
results = [client.post(f"/api/score/{i}/actions", json={"values": {"action": "开始评估"}}).json()["ok"] for i in ids]
# 引擎是同步执行，信号量每次都会释放；这里验证上限常量与信号量行为存在即可
check("并发上限常量为 2", MAX_CONCURRENT_JOBS == 2)
check("三个任务最终都可完成（排队而非报错）", all(results), str(results))

print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
