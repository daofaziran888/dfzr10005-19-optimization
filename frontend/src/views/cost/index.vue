<template>
  <section class="page" data-module="cost">
    <header class="page-head">
      <div>
        <h2>存储成本优化管理</h2>
        <p class="page-desc">维护存储成本的扫描范围、冷数据识别与优化建议追踪，为存储成本优化提供可审计的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记成本分析报告</button>
        <button class="btn" type="button" @click="exportRows">导出存储成本优化清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
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
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" @click="viewEvidenceUsage(row)">证据复用</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无存储成本优化数据，可先登记成本分析报告</td>
        </tr>
      </tbody>
    </table>

    <div v-if="usageDrawer" class="usage-mask" @click.self="usageDrawer = null">
      <div class="usage-drawer">
        <header class="usage-head">
          <h3>证据复用情况 · {{ usageDrawer.ref }}</h3>
          <button class="btn ghost" type="button" @click="usageDrawer = null">关闭</button>
        </header>
        <p v-if="!usageDrawer.items.length" class="usage-empty">
          该成本报告暂未被任何健康度评分复用；评分任务收口引用其「冷数据占比」后会在此显示证据区间与口径版本。
        </p>
        <table v-else class="data-table">
          <thead>
            <tr><th>健康度报告</th><th>评估范围</th><th>证据类别</th><th>复用证据区间</th><th>口径版本</th><th>分段状态</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in usageDrawer.items" :key="item.job_id">
              <td>{{ item['报告编号'] }}</td>
              <td>{{ item['评估范围'] }}</td>
              <td>{{ item.category_label }}</td>
              <td>
                {{ item.evidence_range?.start_ref }} ~ {{ item.evidence_range?.end_ref }}
                （{{ item.evidence_range?.count }} 条 · {{ item.evidence_range?.module }}.{{ item.evidence_range?.field }}）
              </td>
              <td><span class="usage-version">{{ item.spec_version }}</span></td>
              <td>{{ segmentStatusLabel(item.segment_status) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条存储成本优化记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/cost'
const columns = ["报告编号", "分析范围", "存储总量", "冷数据占比", "优化建议", "预计节省", "执行状态", "生成时间", "评分回写摘要"]
const actions = ["开始分析", "执行优化", "归档报告"]
const statuses = ["草稿", "分析中", "已生成", "已执行"]
const stats = [{"label": "成本分析报告总数", "value": 0}, {"label": "已生成报告", "value": 0}, {"label": "已执行报告", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const usageDrawer = ref<{ ref: string; items: any[] } | null>(null)

function segmentStatusLabel(status: string): string {
  return { pending: '待计算', running: '计算中', completed: '已收口', failed: '失败' }[status] ?? status
}

async function viewEvidenceUsage(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/evidence-usage`)
    if (!response.ok) throw new Error('证据复用情况读取失败')
    const payload = await response.json()
    usageDrawer.value = { ref: String(row['报告编号']), items: payload.items ?? [] }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '证据复用情况读取失败'
  }
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '成本分析报告登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('存储成本优化动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '存储成本优化操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('成本分析报告列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '存储成本优化列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.usage-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.45);
  display: flex;
  justify-content: flex-end;
  z-index: 50;
}
.usage-drawer {
  width: 62%;
  max-width: 860px;
  background: #fff;
  height: 100%;
  overflow-y: auto;
  padding: 24px;
  box-shadow: -8px 0 24px rgba(0, 0, 0, 0.12);
}
.usage-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}
.usage-empty {
  padding: 14px 16px;
  background: #f5f7fa;
  color: #5b6472;
  border-radius: 6px;
}
.usage-version {
  font-family: monospace;
  color: #2f6bff;
}
</style>
