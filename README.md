# 数仓治理与运维平台

面向数据仓库团队的治理与运维一体化平台。覆盖表生命周期管理、存储成本优化、数据热度分析、任务依赖编排、SLA告警、资源弹性调度与数仓健康度评估的完整治理链路。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 表生命周期管理 | `lifecycle` | 生命周期策略 | 策略编号、表名称、创建日期 |
| 存储成本优化 | `cost` | 成本分析报告 | 报告编号、分析范围、存储总量 |
| 数据热度分析 | `heatmap` | 热度记录 | 记录编号、表名称、查询次数 |
| 任务依赖编排 | `dag` | 依赖图节点 | 节点编号、节点名称、上游节点 |
| SLA告警管理 | `sla` | SLA规则 | 规则编号、监控对象、产出时间 |
| 资源弹性调度 | `resource` | 资源池配置 | 配置编号、资源类型、最小配额 |
| 数仓健康度评估 | `score` | 健康度报告 | 报告编号、评估范围、存储健康分 |
| 数据漂移检测 | `drift` | 漂移记录 | 记录编号、表名称、漂移字段 |
| 数据资产盘点 | `catalog` | 资产记录 | 资产编号、表名称、资产分类 |
| 数据保留策略 | `retention` | 保留规则 | 规则编号、数据集范围、保留期限 |
| 数据去重管理 | `dedup` | 去重任务 | 任务编号、目标表名、去重字段 |
| 数据分层规划 | `tier` | 分层定义 | 分层编号、分层名称、层级别名 |
| 基线监控管理 | `baseline` | 基线配置 | 基线编号、基线名称、承诺时间 |
| 数据回收站 | `recycle` | 回收记录 | 记录编号、表名称、删除时间 |
| 变更审计追踪 | `audit` | 变更记录 | 记录编号、变更对象、变更类型 |
| 查询性能分析 | `benchmark` | 性能报告 | 报告编号、查询语句、执行耗时 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 健康度分段重算

数仓健康度评估（`score`）不再整份重扫，而是按「评估范围 × 证据类别」分段计算，核心逻辑在
`backend/app/services/scoring_engine.py`。

### 计算段（4 段）

| 计算段 | 健康分字段 | 证据来源 | 证据字段 |
| --- | --- | --- | --- |
| storage | 存储健康分 | 成本报告 `cost`（按分析范围匹配） | 冷数据占比 |
| compute | 计算健康分 | 漂移记录 `drift`（全域） | 偏差比例 |
| quality | 质量健康分 | 去重任务 `dedup`（全域） | 影响行数 |
| composite | 综合评分 | 三类证据段收口后加权（0.4/0.3/0.3） | — |

- 每段独立保存状态（pending/running/completed/failed）、证据区间、证据引用与错误说明；
  已收口（completed）的段在重试或恢复时直接复用，只处理未完成部分。
- 任务绑定口径版本（`spec_version`，见 `CURRENT_SPEC_VERSION`）并固化证据快照。
  口径升级（`POST /api/score/spec/bump`）后新任务按新版本全量重算，旧报告继续使用原快照。
- 同一评估范围同一时刻只允许一个活跃任务（以运行令牌 `running_token` 互斥），重复启动不会
  生成第二份评分；全局并发上限 `MAX_CONCURRENT_JOBS = 2`。
- 每个评估范围保留最近 `SNAPSHOT_KEEP = 3` 个快照版本，超出的只标记淘汰、不删除数据，
  旧报告仍可读取原快照。
- 三类健康分与综合评分必须来自同一证据版本；报告未全部收口时公示会被拒绝，并明确列出
  缺失的证据类别。
- 分段状态、进度与快照分值回写到健康度报告清单；分段结果同时回写到成本/漂移/去重来源行的
  `评分回写` 与 `评分回写摘要` 字段，来源侧还可通过
  `GET /api/{cost|drift|dedup}/{id}/evidence-usage` 查询被复用的证据区间。

### 主要接口

- `POST /api/score/{id}/actions`（action=开始评估/公示报告/归档报告）
- `GET  /api/score/{id}/job`：四个计算段的状态、证据区间、错误与整体进度
- `POST /api/score/{id}/retry`：只重算失败/未收口的计算段
- `POST /api/score/{id}/segments/{segment_key}/retry`：单段重试
- `GET  /api/score/spec`、`POST /api/score/spec/bump`：查询/升级评分口径版本

### 测试

```bash
cd backend
PYTHONPATH=. .venv/bin/python tests/test_scoring.py            # 引擎分段/互斥/快照/空态
PYTHONPATH=. .venv/bin/python tests/test_integration_http.py   # HTTP 级重试/版本/回写/公示拦截
```


领域：数据工程
子方向：数仓与数据湖
