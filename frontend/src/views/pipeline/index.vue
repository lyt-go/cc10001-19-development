<template>
  <section class="page" data-module="pipeline">
    <header class="page-head">
      <div>
        <h2>预处理管道管理</h2>
        <p class="page-desc">定义采集数据的后处理管道，对原始采集结果做去重、清洗、格式化和异常行隔离。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记处理管道</button>
        <button class="btn" type="button" @click="exportRows">导出预处理管道清单</button>
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
          <th>质量门禁</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span :class="['tag', row.质量门禁 === '通过' ? 'tag-ok' : 'tag-block']">
              {{ row.质量门禁 ?? '阻止' }}
            </span>
            <span v-if="reasonText(row)" class="block-reason">{{ reasonText(row) }}</span>
          </td>
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
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无预处理管道数据，可先登记处理管道</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条预处理管道记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/pipeline'
const columns = ["管道编号", "数据来源", "关联审核版本", "字段映射", "去重字段", "清洗表达式", "格式化规则", "输出格式", "异常行策略"]
const actions = ["提交校验", "启用管道", "废弃管道"]
const statuses = ["草稿", "校验中", "已启用", "已废弃"]
const stats = [{"label": "处理管道总数", "value": 0}, {"label": "已启用管道", "value": 0}, {"label": "校验中管道", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

function reasonText(row: Row): string {
  const reasons = row['阻止原因']
  if (Array.isArray(reasons)) return reasons.join('；')
  return typeof reasons === 'string' ? reasons : ''
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '处理管道登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('预处理管道动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '预处理管道操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('处理管道列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '预处理管道列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.tag { display: inline-block; border-radius: 4px; padding: 1px 6px; font-size: 12px; }
.tag-ok { background: #e7f6ec; color: #087443; }
.tag-block { background: #fef3f2; color: #b42318; }
.block-reason { display: block; color: #b42318; font-size: 12px; margin-top: 2px; }
</style>
