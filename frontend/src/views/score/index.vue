<template>
  <section class="page" data-module="score">
    <header class="page-head">
      <div>
        <h2>数仓健康度评估管理</h2>
        <p class="page-desc">按评估范围与证据类别分段重算：存储/计算/质量三类健康分与综合评分来自同一证据版本，未收口报告不能公示。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记健康度报告</button>
        <button class="btn" type="button" @click="exportRows">导出数仓健康度评估清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">当前评分口径</span>
        <strong class="stat-value">{{ specVersion || '—' }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>分段进度</th>
          <th>证据版本</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ formatCell(row, column) }}</td>
          <td>
            <div class="seg-progress">
              <div class="seg-bar">
                <div class="seg-bar-inner" :style="{ width: `${progressPercent(row)}%` }"></div>
              </div>
              <span class="seg-text">{{ progressText(row) }}</span>
              <span v-if="failedNames(row).length" class="seg-failed">缺失：{{ failedNames(row).join('、') }}</span>
            </div>
          </td>
          <td>
            <span v-if="row.evidence_version" class="seg-version">{{ row.evidence_version }}</span>
            <span v-else class="seg-empty">暂无证据版本</span>
          </td>
          <td class="row-actions">
            <button class="link" type="button" @click="runAction('开始评估', row)">开始评估</button>
            <button class="link" type="button" @click="viewJob(row)">分段详情</button>
            <button
              v-if="hasFailedSegment(row)"
              class="link"
              type="button"
              @click="retryAll(row)"
            >重试失败段</button>
            <button class="link" type="button" @click="runAction('公示报告', row)">公示报告</button>
            <button class="link" type="button" @click="runAction('归档报告', row)">归档报告</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 3" class="empty-state">暂无数仓健康度评估数据，可先登记健康度报告</td>
        </tr>
      </tbody>
    </table>

    <!-- 分段详情抽屉 -->
    <div v-if="jobDetail" class="seg-drawer-mask" @click.self="jobDetail = null">
      <div class="seg-drawer">
        <header class="seg-drawer-head">
          <h3>分段重算详情 · {{ jobDetail['报告编号'] }}</h3>
          <button class="btn ghost" type="button" @click="jobDetail = null">关闭</button>
        </header>
        <p class="seg-drawer-meta">
          评估范围：{{ jobDetail['评估范围'] }} ｜ 口径版本：{{ jobDetail.spec_version }} ｜
          进度：{{ jobDetail.progress.completed }}/{{ jobDetail.progress.total }}
        </p>
        <table class="data-table">
          <thead>
            <tr>
              <th>计算段</th><th>状态</th><th>分值</th><th>证据区间</th><th>证据引用</th><th>说明</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="seg in segmentList" :key="seg.key">
              <td>{{ seg.name }}</td>
              <td><span :class="['seg-status', seg.status]">{{ statusLabel(seg.status) }}</span></td>
              <td>{{ seg.score ?? '—' }}</td>
              <td>
                <template v-if="seg.evidence_range">
                  {{ seg.evidence_range.start_ref }} ~ {{ seg.evidence_range.end_ref }}
                  （{{ seg.evidence_range.count }} 条 · {{ seg.evidence_range.module }}.{{ seg.evidence_range.field }}）
                </template>
                <span v-else class="seg-empty">暂无证据</span>
              </td>
              <td>
                <span v-if="evidenceRefsOf(seg.key).length" class="seg-refs">
                  {{ evidenceRefsOf(seg.key).join('、') }}
                </span>
                <span v-else class="seg-empty">—</span>
              </td>
              <td>
                <span v-if="seg.error" class="seg-failed">{{ seg.error }}</span>
                <span v-else class="seg-empty">—</span>
              </td>
              <td>
                <button
                  v-if="seg.status === 'failed' || seg.status === 'pending'"
                  class="link"
                  type="button"
                  @click="retrySegment(seg.key)"
                >重试本段</button>
                <span v-else class="seg-empty">已收口复用</span>
              </td>
            </tr>
          </tbody>
        </table>
        <p v-if="!jobDetail.snapshot" class="seg-warn">
          当前报告尚未收口，三类健康分与综合评分未形成统一证据快照，不能公示。
        </p>
        <p v-else class="seg-ok">
          快照已于 {{ jobDetail.snapshot.frozen_at }} 收口，综合评分 {{ jobDetail.snapshot.composite }}，
          三类健康分与综合评分同属版本 {{ jobDetail.snapshot.spec_version }}。
        </p>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条数仓健康度评估记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type SegmentStatus = 'pending' | 'running' | 'completed' | 'failed'
interface Segment {
  key: string
  name: string
  status: SegmentStatus
  score: number | null
  error: string | null
  evidence_refs?: string[]
  evidence_range: {
    module: string
    field: string
    start_ref: string
    end_ref: string
    count: number
    spec_version: string
  } | null
}
interface JobDetail {
  id: number
  score_entry_id: number
  报告编号: string
  评估范围: string
  spec_version: string
  status: string
  progress: { completed: number; total: number; percent: number }
  segments: Record<string, Segment>
  snapshot: {
    spec_version: string
    composite: number
    frozen_at: string
    evidence_refs: Record<string, string[]>
  } | null
}
type Row = Record<string, any>

const ENDPOINT = '/api/score'
const columns = ["报告编号", "评估范围", "存储健康分", "计算健康分", "质量健康分", "综合评分", "改进建议", "评估时间"]
const stats = [{"label": "健康度报告总数", "value": 0}, {"label": "已生成报告", "value": 0}, {"label": "已公示报告", "value": 0}]
const SEGMENT_NAME: Record<string, string> = {
  storage: '存储健康分',
  compute: '计算健康分',
  quality: '质量健康分',
  composite: '综合评分收口',
}

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const specVersion = ref('')
const jobDetail = ref<JobDetail | null>(null)

const segmentList = computed<Segment[]>(() => {
  if (!jobDetail.value) return []
  return Object.entries(jobDetail.value.segments).map(([key, seg]) => ({
    ...seg,
    key,
    name: seg.name || SEGMENT_NAME[key] || key,
  }))
})

function evidenceRefsOf(key: string): string[] {
  if (!jobDetail.value) return []
  // 优先取分段上固化的引用（证据段收口即有），快照引用作为兜底。
  const segRefs = jobDetail.value.segments[key]?.evidence_refs
  if (segRefs?.length) return segRefs
  return jobDetail.value.snapshot?.evidence_refs?.[key] ?? []
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '健康度报告登记入口尚未接入审批流'
}

function formatCell(row: Row, column: string): string | number {
  const value = row[column]
  if (value === null || value === undefined || value === '') return '—'
  return value
}

function progressPercent(row: Row): number {
  return row.progress?.percent ?? 0
}

function progressText(row: Row): string {
  const p = row.progress
  return p ? `${p.completed}/${p.total}` : '0/4'
}

function failedNames(row: Row): string[] {
  const failed: string[] = row.failed_categories ?? []
  return failed.map((key) => SEGMENT_NAME[key] || key)
}

function hasFailedSegment(row: Row): boolean {
  return failedNames(row).length > 0
}

function statusLabel(status: SegmentStatus): string {
  return { pending: '待计算', running: '计算中', completed: '已收口', failed: '失败' }[status]
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message || '数仓健康度评估动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数仓健康度评估操作失败'
  }
}

async function viewJob(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/job`)
    if (!response.ok) throw new Error('分段详情读取失败')
    jobDetail.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分段详情读取失败'
  }
}

async function retryAll(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/retry`, { method: 'POST', body: '{}' })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '重试失败')
    await reload()
    await viewJob(row)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重试失败'
  }
}

async function retrySegment(segmentKey: string) {
  if (!jobDetail.value) return
  errorMessage.value = ''
  // 重试接口以健康度报告 entry_id（score_entry_id）寻址，不是评分任务表 id。
  const entryId = jobDetail.value.score_entry_id
  try {
    const response = await request(`${ENDPOINT}/${entryId}/segments/${segmentKey}/retry`, {
      method: 'POST',
      body: '{}',
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '分段重试失败')
    const row = rows.value.find((item) => item.id === entryId)
    await reload()
    if (row) await viewJob(row)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分段重试失败'
  }
}

async function loadSpec() {
  try {
    const response = await request(`${ENDPOINT}/spec`)
    if (response.ok) {
      const payload = await response.json()
      specVersion.value = payload.current_spec_version
    }
  } catch {
    specVersion.value = ''
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('健康度报告列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数仓健康度评估列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void loadSpec()
})
</script>

<style scoped>
.seg-progress {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 140px;
}
.seg-bar {
  height: 6px;
  border-radius: 3px;
  background: #e8ecf3;
  overflow: hidden;
}
.seg-bar-inner {
  height: 100%;
  background: #2f6bff;
  transition: width 0.3s ease;
}
.seg-text {
  font-size: 12px;
  color: #5b6472;
}
.seg-failed {
  color: #d83931;
  font-size: 12px;
}
.seg-empty {
  color: #98a0ad;
  font-size: 12px;
}
.seg-version {
  font-family: monospace;
  color: #2f6bff;
}
.seg-refs {
  font-size: 12px;
  color: #333c48;
}
.seg-status {
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 12px;
}
.seg-status.completed {
  background: #e6f6ec;
  color: #1a7f37;
}
.seg-status.failed {
  background: #fde8e7;
  color: #d83931;
}
.seg-status.running {
  background: #e8f0ff;
  color: #2f6bff;
}
.seg-status.pending {
  background: #f0f1f4;
  color: #8a929e;
}
.seg-drawer-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.45);
  display: flex;
  justify-content: flex-end;
  z-index: 50;
}
.seg-drawer {
  width: 70%;
  max-width: 980px;
  background: #fff;
  height: 100%;
  overflow-y: auto;
  padding: 24px;
  box-shadow: -8px 0 24px rgba(0, 0, 0, 0.12);
}
.seg-drawer-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}
.seg-drawer-meta {
  color: #5b6472;
  font-size: 13px;
  margin-bottom: 16px;
}
.seg-warn {
  margin-top: 16px;
  padding: 10px 14px;
  background: #fde8e7;
  color: #d83931;
  border-radius: 6px;
}
.seg-ok {
  margin-top: 16px;
  padding: 10px 14px;
  background: #e6f6ec;
  color: #1a7f37;
  border-radius: 6px;
}
</style>
