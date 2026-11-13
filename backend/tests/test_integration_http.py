"""HTTP 级集成验证：失败重试收口、口径版本并存、同范围并发互斥。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.services.scoring_engine import MAX_CONCURRENT_JOBS, engine
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


print("== D. 证据补齐后重试收口（v2 口径下）==")
# 先在该评估范围登记成本报告（API 只持久化必填字段），再在同进程补写冷数据占比证据
cost_created = client.post("/api/cost", json={"values": {"报告编号": "COST-0009", "分析范围": "新增湖仓一体范围", "存储总量": "60TB"}})
check("成本报告已登记", cost_created.json()["ok"], cost_created.text)
# SCOR-0003 先跑一次，storage 因无可用冷数据占比失败
client.post("/api/score/3/actions", json={"values": {"action": "开始评估"}})
job = client.get("/api/score/3/job").json()
check("初始 storage 失败", job["segments"]["storage"]["status"] == "failed")
# 业务侧证据产出：直接在同进程 store 给该范围补可解析冷数据占比
for row in store.rows("cost"):
    if row.get("报告编号") == "COST-0009":
        row["冷数据占比"] = "45%"
check("证据已写入 store", any(r.get("冷数据占比") == "45%" for r in store.rows("cost")))
retry = client.post("/api/score/3/retry", json={})
check("整体重试 ok", retry.json()["ok"], retry.text)
entry = retry.json()["entry"]
job = client.get("/api/score/3/job").json()
check("storage 收口", job["segments"]["storage"]["status"] == "completed")
check("综合收口段补齐", job["segments"]["composite"]["status"] == "completed")
check("报告状态已生成", entry["status"] == "已生成", entry["status"])
check("报告带证据版本", entry["evidence_version"] == job["spec_version"], str(entry.get("evidence_version")))
check("存储健康分=55（100-45）", entry["存储健康分"] == 55.0, str(entry["存储健康分"]))

print("== E. 口径升级：新版本重算，旧快照保留 ==")
# 用 D 中已收口的 SCOR-0003 作为 v1 基线报告
baseline_job = client.get("/api/score/3/job").json()
before_version = baseline_job["spec_version"]
old_composite = baseline_job["snapshot"]["composite"]
baseline_report = client.get("/api/score/3").json()
check("基线报告绑定旧证据版本", baseline_report["evidence_version"] == before_version)
new_version = client.post("/api/score/spec/bump", json={"values": {}}).json()["entry"]["spec_version"]
check("口径版本升级", new_version != before_version, new_version)
spec = client.get("/api/score/spec").json()
check("/spec 实时反映新版本", spec["current_spec_version"] == new_version, str(spec))
# 同一条报告在新口径下重新评估 -> 新建新口径任务并全量重算
client.post("/api/score/3/actions", json={"values": {"action": "开始评估"}})
new_job = client.get("/api/score/3/job").json()
check("新任务采用新口径", new_job["spec_version"] == new_version)
# 旧口径任务的快照仍保留，分值不变
old_jobs = [j for j in engine.list_jobs() if j["score_entry_id"] == 3 and not j.get("legacy")]
preserved = next((j for j in old_jobs if j["spec_version"] == before_version), None)
check("旧任务快照未被清除且分值不变", preserved is not None and preserved["snapshot"]["composite"] == old_composite)
# 报告当前展示新口径结果，证据引用同属新版本
latest_report = client.get("/api/score/3").json()
check("报告刷新后指向新证据版本", latest_report["evidence_version"] == new_version, str(latest_report.get("evidence_version")))
check("报告综合分与新快照一致", latest_report["综合评分"] == new_job["snapshot"]["composite"])

print("== F. 同范围并发互斥（进程内模拟活跃令牌）==")
created = client.post("/api/score", json={"values": {"报告编号": "SCOR-0300", "评估范围": "互斥验证范围", "存储健康分": "0"}})
mutex_id = created.json()["entry"]["id"]
active = engine._job_of_entry(mutex_id)
active["running_token"] = "sim-lock-mutex"
engine._running_scopes.add("互斥验证范围")
denied = client.post(f"/api/score/{mutex_id}/actions", json={"values": {"action": "开始评估"}})
check("活跃任务时重复启动被拒", denied.json()["ok"] is False and "进行中" in denied.json()["message"], denied.text)
# 令牌释放后可正常启动
active["running_token"] = None
engine._running_scopes.discard("互斥验证范围")
allowed = client.post(f"/api/score/{mutex_id}/actions", json={"values": {"action": "开始评估"}})
check("令牌释放后可启动", allowed.json()["ok"], allowed.text)

print("== G. 快照保留策略：同范围超过 3 个版本只标记淘汰不删数据 ==")
# 先给该范围准备可解析成本证据，保证每个口径任务都能收口出快照
client.post("/api/cost", json={"values": {"报告编号": "COST-KEEP", "分析范围": "保留策略范围", "存储总量": "30TB"}})
for row in store.rows("cost"):
    if row.get("报告编号") == "COST-KEEP":
        row["冷数据占比"] = "28%"
for idx in range(4):
    client.post("/api/score/spec/bump", json={"values": {}})
    rid = client.post("/api/score", json={"values": {"报告编号": f"SCOR-K{idx}", "评估范围": "保留策略范围", "存储健康分": "0"}}).json()["entry"]["id"]
    client.post(f"/api/score/{rid}/actions", json={"values": {"action": "开始评估"}})
scope_jobs = [j for j in engine.list_jobs() if j["评估范围"] == "保留策略范围" and j.get("snapshot")]
retired = [j for j in scope_jobs if j.get("snapshot_retired")]
check("产生 4 个快照任务", len(scope_jobs) == 4, str(len(scope_jobs)))
check("超出保留数的旧快照被标记淘汰", len(retired) == 1, str(len(retired)))
check("被淘汰快照数据仍保留（旧报告可读）", all(j["snapshot"] is not None for j in retired))

print("== H. 未收口公示拦截的缺失类别说明 ==")
created = client.post("/api/score", json={"values": {"报告编号": "SCOR-0400", "评估范围": "公示拦截范围", "存储健康分": "0"}})
pub_id = created.json()["entry"]["id"]
client.post(f"/api/score/{pub_id}/actions", json={"values": {"action": "开始评估"}})
pub = client.post(f"/api/score/{pub_id}/actions", json={"values": {"action": "公示报告"}})
check("未收口公示被拒", pub.json()["ok"] is False)
# 该范围无成本证据，存储段缺失；提示必须明确点出缺失类别而不是笼统报错
check("拒绝信息明确列出缺失类别", "缺失类别" in pub.json()["message"] and "存储健康分" in pub.json()["message"], pub.json()["message"])

print("== I. 状态结果回写到成本/漂移/去重来源清单 ==")
# 先让 SCOR-0002 收口（其证据来自 COST-0002 / DRIF-0002 / DEDU-0002）
client.post("/api/score/2/actions", json={"values": {"action": "开始评估"}})
# 用 SCOR-0002（已收口）验证三类来源行都带回写摘要与明细
cost_row = next(r for r in store.rows("cost") if r.get("报告编号") == "COST-0002")
drift_row = next(r for r in store.rows("drift") if r.get("记录编号") == "DRIF-0002")
dedup_row = next(r for r in store.rows("dedup") if r.get("任务编号") == "DEDU-0002")
check("成本行带回写摘要", "SCOR-0002" in (cost_row.get("评分回写摘要") or ""), cost_row.get("评分回写摘要"))
check("漂移行带回写摘要", "计算健康分" in (drift_row.get("评分回写摘要") or ""), drift_row.get("评分回写摘要"))
check("去重行带回写摘要", "质量健康分" in (dedup_row.get("评分回写摘要") or ""), dedup_row.get("评分回写摘要"))
cost_wb = cost_row["评分回写"][0]
current_spec = client.get("/api/score/spec").json()["current_spec_version"]
check("回写明细含综合分与口径版本", cost_wb.get("综合评分") == 71.3 and cost_wb.get("spec_version") == current_spec, str(cost_wb))
# 同任务重算覆盖旧回写，不产生重复条目
client.post("/api/score/2/actions", json={"values": {"action": "开始评估"}})
check("同任务回写不重复", len([w for w in cost_row["评分回写"] if w["job_id"] == cost_wb["job_id"]]) == 1)
# 不同评分任务引用同一证据时各自保留
second = client.post("/api/score", json={"values": {"报告编号": "SCOR-0500", "评估范围": "核心数仓全域", "存储健康分": "0"}}).json()["entry"]["id"]
client.post(f"/api/score/{second}/actions", json={"values": {"action": "开始评估"}})
check("多任务引用各自保留回写", len(cost_row["评分回写"]) >= 2, str(len(cost_row["评分回写"])))

print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
