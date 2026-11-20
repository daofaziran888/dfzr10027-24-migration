<template>
  <section class="page" data-module="data_mart_storage">
    <header class="page-head">
      <div>
        <h2>集市存储上线</h2>
        <p class="page-desc">
          主题目录与实验统计记录分离：主题配置行带数字编号，统计记录按实验与业务日期追溯，
          支持迁移批次快照、读端回滚与实验复盘排序。
        </p>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">当前读端模式</span>
        <strong class="stat-value" data-testid="read-mode">{{ state.read_mode ?? '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">主题目录（topic）</span>
        <strong class="stat-value">{{ state.topic_count ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">统计记录（experiment_stat）</span>
        <strong class="stat-value">{{ state.statistic_count ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">迁移批次</span>
        <strong class="stat-value">{{ batches.length }}</strong>
      </article>
    </div>

    <div class="filter-bar">
      <button class="btn primary" data-testid="migrate" type="button" @click="runMigration">提交迁移</button>
      <button class="btn" data-testid="rollback" type="button" @click="runRollback">回滚读端</button>
      <button class="btn ghost" type="button" @click="refreshAll">刷新</button>
      <span v-if="message" class="page-desc" data-testid="action-message">{{ message }}</span>
    </div>

    <h3>行类型来源构成</h3>
    <table class="data-table" data-testid="provenance-table">
      <tbody>
        <tr><th>主题来源表</th><td>{{ state.topic_source ?? '—' }}</td><th>行类型</th><td>{{ state.topic_row_type ?? '—' }}</td></tr>
        <tr><th>统计来源表</th><td>{{ state.statistic_source ?? '—' }}</td><th>行类型</th><td>{{ state.statistic_row_type ?? '—' }}</td></tr>
        <tr><th>覆盖实验</th><td>{{ (state.experiments ?? []).join('、') || '—' }}</td><th>业务日期</th><td>{{ (state.business_dates ?? []).join('、') || '—' }}</td></tr>
        <tr><th>样本日期</th><td colspan="3">{{ (state.sample_dates ?? []).join('、') || '—' }}</td></tr>
      </tbody>
    </table>

    <h3>迁移批次（含主题数据快照与校验和）</h3>
    <table class="data-table" data-testid="batch-table">
      <thead>
        <tr><th>批次号</th><th>迁移时间</th><th>覆盖实验</th><th>主题数</th><th>统计数</th><th>快照校验和</th></tr>
      </thead>
      <tbody>
        <tr v-for="batch in batches" :key="batch.batch_no" :data-testid="`batch-${batch.batch_no}`">
          <td>{{ batch.batch_no }}</td>
          <td>{{ batch.migrated_at }}</td>
          <td>{{ (batch.experiment_scope ?? []).join('、') || '全量' }}</td>
          <td>{{ batch.snapshot?.topic_count }}</td>
          <td>{{ batch.snapshot?.stat_count }}</td>
          <td class="checksum">{{ batch.checksum }}</td>
        </tr>
        <tr v-if="!batches.length"><td colspan="6" class="empty-state">暂无迁移批次</td></tr>
      </tbody>
    </table>

    <h3>主题目录 / 统计记录</h3>
    <div class="filter-bar">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="btn"
        :class="{ primary: activeTab === tab.key }"
        :data-testid="`tab-${tab.key}`"
        type="button"
        @click="switchTab(tab.key)"
      >
        {{ tab.label }}
      </button>
    </div>
    <table v-if="activeTab === 'topics'" class="data-table" data-testid="topics-table">
      <thead>
        <tr><th>数字 id</th><th>主题编号</th><th>主题名称</th><th>业务域</th><th>行类型</th><th>状态</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in topics" :key="String(row.id)" :data-testid="`topic-row-${row.id}`">
          <td>{{ row.id }}</td>
          <td>{{ row['主题编号'] }}</td>
          <td>{{ row['主题名称'] }}</td>
          <td>{{ row['业务域'] }}</td>
          <td>{{ row.row_type }}</td>
          <td>{{ row.status }}</td>
        </tr>
        <tr v-if="!topics.length"><td colspan="6" class="empty-state">暂无主题配置行</td></tr>
      </tbody>
    </table>
    <table v-else class="data-table" data-testid="statistics-table">
      <thead>
        <tr><th>实验编号</th><th>旧主题编号</th><th>业务日期</th><th>样本日期</th><th>相对提升</th><th>显著性</th><th>行类型</th><th>来源表</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in statistics" :key="String(row.stat_id ?? `${row['实验编号']}-${row['业务日期']}`)"
            :data-testid="`stat-row-${row['实验编号']}`">
          <td>{{ row['实验编号'] }}</td>
          <td>{{ `DATA-STAT-${row['实验编号']}` }}</td>
          <td>{{ row['业务日期'] }}</td>
          <td>{{ row['样本日期'] }}</td>
          <td>{{ row['相对提升'] }}</td>
          <td>{{ row['显著性'] ? '显著' : '不显著' }}</td>
          <td>{{ row.row_type }}</td>
          <td>{{ state.statistic_source }}</td>
        </tr>
        <tr v-if="!statistics.length"><td colspan="8" class="empty-state">暂无实验统计记录</td></tr>
      </tbody>
    </table>

    <h3>实验复盘报表（按相对提升降序）</h3>
    <div class="filter-bar">
      <label class="filter-item">
        <span>复盘业务日期（留空取最新业务日）</span>
        <input v-model="reviewDate" data-testid="review-date" type="date" />
      </label>
      <button class="btn primary" data-testid="build-review" type="button" @click="buildReview">生成复盘报表</button>
    </div>
    <table class="data-table" data-testid="review-table">
      <thead>
        <tr><th>报表号</th><th>业务日期</th><th>生成时读端</th><th>复盘顺序</th><th>覆盖实验/样本日期</th><th>操作</th></tr>
      </thead>
      <tbody>
        <tr v-for="report in reviews" :key="report.report_no" :data-testid="`review-${report.report_no}`">
          <td>{{ report.report_no }}</td>
          <td>{{ report.business_date }}</td>
          <td>{{ report.read_mode_at_creation }}</td>
          <td data-testid="review-order">{{ report.order.join(' → ') }}</td>
          <td>
            <span v-for="exp in report.coverage?.experiments ?? []" :key="exp" class="coverage-chip">
              {{ exp }}（样本 {{ report.coverage?.sample_dates?.[exp] }}）
            </span>
          </td>
          <td class="row-actions">
            <button class="link" type="button" :data-testid="`open-${report.report_no}`" @click="openStatistics(report.report_no)">
              打开关联统计
            </button>
          </td>
        </tr>
        <tr v-if="!reviews.length"><td colspan="6" class="empty-state">暂无复盘报表</td></tr>
      </tbody>
    </table>

    <div v-if="linkedStatistics.length" data-testid="linked-panel">
      <h3>关联实验统计（{{ activeReport }}）</h3>
      <table class="data-table">
        <thead>
          <tr><th>排名</th><th>实验编号</th><th>旧主题编号</th><th>样本日期</th><th>业务日期</th><th>冻结转化人数(实验/对照)</th><th>当前可见</th><th>创建时来源</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in linkedStatistics" :key="item['实验编号']" :data-testid="`linked-${item['实验编号']}`">
            <td>{{ item.rank }}</td>
            <td>{{ item['实验编号'] }}</td>
            <td>{{ item['旧主题编号'] }}</td>
            <td>{{ item['样本日期'] }}</td>
            <td>{{ item['业务日期'] }}</td>
            <td>{{ item.frozen_stat?.['实验组转化人数'] }} / {{ item.frozen_stat?.['控制组转化人数'] }}</td>
            <td>{{ item.current_visible ? '是' : '否' }}</td>
            <td>{{ item.source_at_creation }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <footer class="page-foot">
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type StoreState = {
  read_mode: string
  topic_source: string
  topic_row_type: string
  topic_count: number
  statistic_source: string
  statistic_row_type: string
  statistic_count: number
  experiments: string[]
  business_dates: string[]
  sample_dates: string[]
}

type Batch = {
  batch_no: string
  migrated_at: string
  experiment_scope: string[]
  checksum: string
  snapshot: { topic_count: number; stat_count: number }
}

type Row = Record<string, string | number | boolean | null>

type Review = {
  report_no: string
  business_date: string
  read_mode_at_creation: string
  order: string[]
  coverage?: {
    experiments: string[]
    sample_dates: Record<string, string>
  }
}

type LinkedStat = {
  rank: number
  实验编号: string
  旧主题编号: string
  样本日期: string
  业务日期: string
  source_at_creation: string
  current_visible: boolean
  frozen_stat?: Row
}

const tabs = [
  { key: 'topics', label: '主题目录' },
  { key: 'statistics', label: '统计记录' },
] as const

type TabKey = (typeof tabs)[number]['key']

const state = ref<Partial<StoreState>>({})
const batches = ref<Batch[]>([])
const topics = ref<Row[]>([])
const statistics = ref<Row[]>([])
const reviews = ref<Review[]>([])
const linkedStatistics = ref<LinkedStat[]>([])
const activeTab = ref<TabKey>('topics')
const activeReport = ref('')
const reviewDate = ref('')
const message = ref('')
const errorMessage = ref('')

async function getJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return (await response.json()) as T
}

async function postAction(path: string): Promise<{ ok: boolean; message: string; entry?: unknown }> {
  const response = await request(path, { method: 'POST' })
  return (await response.json()) as { ok: boolean; message: string; entry?: unknown }
}

async function loadState() {
  state.value = await getJson<StoreState>('/api/data_mart/storage/state')
}

async function loadBatches() {
  const payload = await getJson<{ items: Batch[] }>('/api/data_mart/storage/migrations')
  batches.value = payload.items
}

async function loadTopics() {
  const payload = await getJson<{ items: Row[] }>('/api/data_mart/topics')
  topics.value = payload.items
}

async function loadStatistics() {
  const payload = await getJson<{ items: Row[] }>('/api/data_mart/statistics')
  statistics.value = payload.items
}

async function loadReviews() {
  const payload = await getJson<{ items: Review[] }>('/api/data_mart/reviews')
  reviews.value = payload.items
}

async function switchTab(tab: TabKey) {
  activeTab.value = tab
  if (tab === 'topics') await loadTopics()
  else await loadStatistics()
}

async function runMigration() {
  errorMessage.value = ''
  try {
    const result = await postAction('/api/data_mart/storage/migrate')
    message.value = result.message
    await refreshAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '迁移失败'
  }
}

async function runRollback() {
  errorMessage.value = ''
  try {
    const result = await postAction('/api/data_mart/storage/rollback')
    message.value = result.message
    await refreshAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '回滚失败'
  }
}

async function buildReview() {
  errorMessage.value = ''
  try {
    const query = reviewDate.value ? `?business_date=${encodeURIComponent(reviewDate.value)}` : ''
    const response = await request(`/api/data_mart/reviews${query}`, { method: 'POST' })
    const payload = (await response.json()) as { ok: boolean; message: string }
    if (!payload.ok) throw new Error(payload.message)
    message.value = payload.message
    await loadReviews()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '复盘报表生成失败'
  }
}

async function openStatistics(reportNo: string) {
  errorMessage.value = ''
  try {
    const payload = await getJson<{ items: LinkedStat[] }>(
      `/api/data_mart/reviews/${reportNo}/statistics`)
    activeReport.value = reportNo
    linkedStatistics.value = payload.items
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '关联统计读取失败'
  }
}

async function refreshAll() {
  await Promise.all([loadState(), loadBatches(), loadReviews()])
  if (activeTab.value === 'topics') await loadTopics()
  else await loadStatistics()
}

onMounted(refreshAll)
</script>

<style scoped>
.checksum {
  font-family: monospace;
  font-size: 12px;
  word-break: break-all;
}

.coverage-chip {
  display: inline-block;
  margin-right: 8px;
  padding: 2px 8px;
  background: #eef2ff;
  border-radius: 10px;
  font-size: 12px;
}

h3 {
  margin: 18px 0 8px;
  font-size: 15px;
}
</style>
