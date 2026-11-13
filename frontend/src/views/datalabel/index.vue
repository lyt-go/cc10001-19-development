<template>
  <section class="page" data-module="datalabel">
    <header class="page-head">
      <div>
        <h2>数据标注管理</h2>
        <p class="page-desc">管理采集数据的标注任务：按字段列表与记录数阈值抽检，记录通过、失败与需复核数量，审核通过后才允许进入预处理与输出。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记标注任务</button>
        <button class="btn" type="button" @click="exportRows">导出数据标注清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in statCards" :key="item.label" class="stat-card">
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
          <th>质量门禁</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span :class="['tag', row.交付状态 === '允许' ? 'tag-ok' : 'tag-block']">
              {{ row.交付状态 ?? '阻止' }}
            </span>
            <span v-if="row.阻止原因 && row.阻止原因.length" class="block-reason">
              {{ row.阻止原因.join('；') }}
            </span>
          </td>
          <td class="row-actions">
            <button class="link" type="button" @click="openInspect(row)">抽检</button>
            <button class="link" type="button" @click="openRecheck(row)">复核</button>
            <button class="link" type="button" @click="openAudit(row)">审核</button>
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无数据标注数据，可先登记标注任务</td>
        </tr>
      </tbody>
    </table>

    <section v-if="active" class="panel">
      <header class="panel-head">
        <h3>{{ activeTitle }} · {{ active.标注编号 }}</h3>
        <span>
          当前审核版本 v{{ active.audit_version ?? '—' }} ·
          通过 {{ active.counts.passed }} / 失败 {{ active.counts.failed }} / 需复核 {{ active.counts.review }}
        </span>
      </header>

      <div v-if="active.reasons.length" class="panel-alert">
        <strong>阻止原因：</strong>
        <span>{{ active.reasons.join('；') }}</span>
      </div>

      <div class="form-grid">
        <label v-for="field in inspectFormFields" :key="field" class="filter-item">
          <span>{{ field }}</span>
          <input v-model="inspectForm[field]" :placeholder="`按 ${field} 填写`" />
        </label>
      </div>

      <div class="panel-actions">
        <button class="btn primary" type="button" @click="submitInspect" :disabled="busy">提交抽检</button>
        <button class="btn" type="button" @click="submitRecheck" :disabled="busy">从失败样本复核</button>
        <button class="btn" type="button" @click="submitAudit('通过')" :disabled="busy">审核通过</button>
        <button class="btn" type="button" @click="submitAudit('不通过')" :disabled="busy">审核不通过</button>
        <button class="btn ghost" type="button" @click="loadDetail">刷新明细</button>
        <button class="btn ghost" type="button" @click="active = null">收起</button>
      </div>

      <div v-if="active.failures.length" class="failure-list">
        <h4>未通过样本（字段名与原因）</h4>
        <table class="data-table">
          <thead>
            <tr>
              <th>样本</th>
              <th>字段</th>
              <th>原因</th>
              <th>当前值</th>
              <th>修正值</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in active.failures" :key="`${item.sample_id}-${item.field}-${item.reason}`">
              <td>{{ item.sample_id }}</td>
              <td>{{ item.field }}</td>
              <td>{{ item.reason }}</td>
              <td>{{ item.value ?? '—' }}</td>
              <td>
                <input v-model="recheckValues[`${item.sample_id}.${item.field}`]" placeholder="填写修正值" />
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="active.audits && active.audits.length" class="history-list">
        <h4>审核历史（最新一次有效版本为准）</h4>
        <ul>
          <li v-for="item in active.audits" :key="String(item.审核版本)">
            v{{ item.审核版本 }} · 第 {{ item.抽检轮次 }} 轮 ·
            {{ item.审核结论 }} · {{ item.有效 ? '当前有效' : '历史' }} ·
            通过 {{ item.passed }} / 失败 {{ item.failed }} / 需复核 {{ item.review }} ·
            {{ item.审核人 }} · {{ item.确认时间 }}
          </li>
        </ul>
      </div>

      <div v-if="active.rounds && active.rounds.length" class="history-list">
        <h4>抽检轮次（退回标注后保留本轮结果）</h4>
        <ul>
          <li v-for="item in active.rounds" :key="String(item.轮次)">
            第 {{ item.轮次 }} 轮 · {{ item.判定.verdict }} ·
            通过 {{ item.判定.passed }} / 失败 {{ item.判定.failed }} / 需复核 {{ item.判定.review }} ·
            状态 {{ item.状态 }} · 复核 {{ item.复核次数 }} 次
          </li>
        </ul>
      </div>

      <ul v-if="detailMessages.length" class="history-list">
        <li v-for="message in detailMessages" :key="message">{{ message }}</li>
      </ul>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条数据标注记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/datalabel'
const columns = ["标注编号", "数据集名称", "标注类型", "字段列表", "标注人", "审核人", "总条目数", "完成条目数", "质量结论"]
const actions = ["开始标注", "提交审核", "废弃任务"]
const filterFields = columns.slice(0, 3)
const inspectFormFields = ["抽检人", "审核人", "校验编号", "记录数阈值", "样本条数"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const statCards = ref([
  { label: '标注任务总数', value: 0 },
  { label: '标注中任务', value: 0 },
  { label: '已完成任务', value: 0 },
  { label: '通过质量门禁', value: 0 },
  { label: '被阻止交付', value: 0 },
  { label: '需复核样本', value: 0 },
])
const active = ref<Row | null>(null)
const activeTitle = ref('抽检与审核')
const detailMessages = ref<string[]>([])
const inspectForm = ref<Record<string, string>>({})
const recheckValues = ref<Record<string, string>>({})
const busy = ref(false)

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '标注任务登记入口尚未接入审批流'
}

function openInspect(row: Row) {
  inspectForm.value = { 抽检人: row.标注人 ?? '', 审核人: row.审核人 ?? '', 校验编号: row.校验编号 ?? '' }
  recheckValues.value = {}
  void loadDetail(row, '抽检与审核')
}

function openRecheck(row: Row) {
  recheckValues.value = {}
  void loadDetail(row, '失败样本复核')
}

function openAudit(row: Row) {
  void loadDetail(row, '审核确认')
}

async function loadDetail(row: Row, title = activeTitle.value) {
  activeTitle.value = title
  detailMessages.value = []
  try {
    const payload = await fetchJson(`${ENDPOINT}/${row.id}/inspections`)
    active.value = {
      ...row,
      ...payload.gate,
      rounds: payload.rounds ?? [],
      audits: payload.audits ?? [],
      failures: payload.gate?.failures ?? [],
    }
    const latest = (payload.rounds ?? [])[payload.rounds?.length - 1]
    const detail = active.value
    if (latest && detail) {
      detail.failures = flattenFailures(latest?.判定?.failures ?? [])
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '抽检明细读取失败'
  }
}

function flattenFailures(items: Row[]): Row[] {
  return items ?? []
}

async function fetchJson(path: string): Promise<any> {
  const response = await request(path)
  if (!response.ok) {
    throw new Error('抽检明细读取失败')
  }
  return response.json()
}

function parseSamples(): Row[] | undefined {
  // 演示环境由后端从任务样本池按记录数阈值抽取；此处仅在需要时透传显式样本。
  return undefined
}

async function submitInspect() {
  if (!active.value) return
  busy.value = true
  try {
    const values: Record<string, any> = {}
    for (const field of ['抽检人', '审核人', '校验编号']) {
      if (inspectForm.value[field]) values[field] = inspectForm.value[field]
    }
    if (inspectForm.value.记录数阈值) values['记录数阈值'] = inspectForm.value.记录数阈值
    const samples = parseSamples()
    if (samples) values.samples = samples
    const result = await postAction(`${ENDPOINT}/${active.value.id}/inspections`, values)
    detailMessages.value = [result.message]
    await loadDetail(active.value)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '抽检提交失败'
  } finally {
    busy.value = false
  }
}

async function submitRecheck() {
  if (!active.value) return
  busy.value = true
  try {
    const sampleValues: Record<string, Record<string, string>> = {}
    for (const [key, value] of Object.entries(recheckValues.value)) {
      if (!value) continue
      const [sampleId, field] = key.split('.')
      sampleValues[sampleId] = { ...(sampleValues[sampleId] ?? {}), [field]: value }
    }
    if (!Object.keys(sampleValues).length) {
      throw new Error('请先填写至少一个失败样本的修正值')
    }
    const round = lastRound(active.value)
    const result = await postAction(
      `${ENDPOINT}/${active.value.id}/inspections/${round}/recheck`,
      { 审核人: inspectForm.value.审核人, sample_values: sampleValues },
    )
    detailMessages.value = [result.message]
    recheckValues.value = {}
    await loadDetail(active.value)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '复核提交失败'
  } finally {
    busy.value = false
  }
}

async function submitAudit(verdict: '通过' | '不通过') {
  if (!active.value) return
  busy.value = true
  try {
    const round = lastRound(active.value)
    const result = await postAction(`${ENDPOINT}/${active.value.id}/audits`, {
      审核人: inspectForm.value.审核人 || active.value.审核人,
      抽检轮次: round,
      审核结论: verdict,
    })
    detailMessages.value = [result.message]
    await loadDetail(active.value)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '审核提交失败'
  } finally {
    busy.value = false
  }
}

function lastRound(row: Row): number | undefined {
  const rounds = row.rounds ?? []
  return rounds.length ? rounds[rounds.length - 1].轮次 : undefined
}

async function postAction(path: string, values: Record<string, any>): Promise<Row> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  const payload = await response.json()
  if (!payload.ok) {
    throw new Error(payload.message ?? '操作未生效')
  }
  return payload
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const result = await postAction(`${ENDPOINT}/${row.id}/actions`, { action })
    detailMessages.value = [result.message]
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据标注操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const payload = await fetchJson(`${ENDPOINT}?${query}`)
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    const stats = await fetchJson(`${ENDPOINT}/stats`)
    statCards.value = [
      { label: '标注任务总数', value: stats.标注任务总数 ?? 0 },
      { label: '标注中任务', value: stats.标注中任务 ?? 0 },
      { label: '已完成任务', value: stats.已完成任务 ?? 0 },
      { label: '通过质量门禁', value: stats.通过质量门禁 ?? 0 },
      { label: '被阻止交付', value: stats.被阻止交付 ?? 0 },
      { label: '需复核样本', value: stats.需复核样本 ?? 0 },
    ]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据标注列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.panel { margin-top: 16px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; }
.panel-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; }
.panel-alert { background: #fef3f2; border: 1px solid #fecdca; color: #b42318; border-radius: 6px; padding: 8px 10px; font-size: 13px; margin: 8px 0; }
.form-grid { display: flex; flex-wrap: wrap; gap: 10px; margin: 10px 0; }
.panel-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; }
.failure-list, .history-list { margin-top: 12px; font-size: 13px; }
.failure-list table input { width: 100%; padding: 4px 6px; border: 1px solid var(--border); border-radius: 4px; }
.tag { display: inline-block; border-radius: 4px; padding: 1px 6px; font-size: 12px; }
.tag-ok { background: #e7f6ec; color: #087443; }
.tag-block { background: #fef3f2; color: #b42318; }
.block-reason { display: block; color: #b42318; font-size: 12px; margin-top: 2px; }
</style>
