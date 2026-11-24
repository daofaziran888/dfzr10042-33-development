<template>
  <section class="page">
    <header class="page-head">
      <div>
        <h2>地区发布门槛</h2>
        <p class="page-desc">
          按各省有效订单数核定发布资格：样本不足的省份不单独成项，上级区域仅在负责人确认后按区级汇总披露。
        </p>
      </div>
      <div class="page-actions">
        <label class="filter-item">
          <span>省级最少订单</span>
          <input v-model.number="provinceMin" type="number" min="1" data-testid="province-min" />
        </label>
        <label class="filter-item">
          <span>区级最少订单</span>
          <input v-model.number="regionMin" type="number" min="1" data-testid="region-min" />
        </label>
        <label class="filter-item checkbox">
          <input v-model="includeMetrics" type="checkbox" data-testid="include-metrics" />
          <span>显示内部全量指标</span>
        </label>
        <button class="btn primary" data-testid="reload" type="button" @click="reload">重新核定</button>
      </div>
    </header>

    <div v-if="errorMessage" class="error-text">{{ errorMessage }}</div>

    <div class="stat-row">
      <article v-for="card in cards" :key="card.label" class="stat-card">
        <span class="stat-label">{{ card.label }}</span>
        <strong class="stat-value">{{ card.value }}</strong>
      </article>
    </div>

    <h3 class="section-title">发布报告（只含已允许地区）</h3>
    <table class="data-table" data-testid="report-table">
      <thead>
        <tr>
          <th>层级</th>
          <th>区域</th>
          <th>条目</th>
          <th>有效订单数</th>
          <th>金额</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in report.rows" :key="`${row.level}-${row.region}-${row.label}`">
          <td>{{ row.level === 'region' ? '区级汇总' : '省级' }}</td>
          <td>{{ row.region }}</td>
          <td>{{ row.label }}</td>
          <td>{{ row.order_count }}</td>
          <td>{{ row.amount }}</td>
        </tr>
        <tr v-if="!report.rows.length">
          <td colspan="5" class="empty-state">本轮没有可披露的地区</td>
        </tr>
      </tbody>
      <tfoot>
        <tr>
          <th colspan="3">合计（仅已披露行）</th>
          <th data-testid="report-total-orders">{{ report.totals.order_count }}</th>
          <th data-testid="report-total-amount">{{ report.totals.amount }}</th>
        </tr>
      </tfoot>
    </table>

    <p class="status-legend">
      入算地区：{{ report.included_regions.join('、') || '无' }}；
      入算省份：{{ report.included_provinces.join('、') || '无' }}；
      待确认：{{ report.pending_confirmations.join('、') || '无' }}
    </p>

    <h3 class="section-title">发布资格核定</h3>
    <table class="data-table" data-testid="decision-table">
      <thead>
        <tr>
          <th>层级</th>
          <th>名称</th>
          <th>所属区域</th>
          <th>有效订单数</th>
          <th>是否达标</th>
          <th>是否披露</th>
          <th>原因</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in decisionRows" :key="`${row.level}-${row.name}`">
          <td>{{ row.level === 'region' ? '区级' : '省级' }}</td>
          <td>{{ row.name }}</td>
          <td>{{ row.region }}</td>
          <td>{{ row.order_count ?? '—' }}</td>
          <td>{{ row.eligible ? '达标' : '未达标' }}</td>
          <td>{{ row.allowed ? '已披露' : '未披露' }}</td>
          <td>{{ reasonLabel(row.reason) }}</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">未披露项（仅名称与原因）</h3>
    <ul class="suppressed-list" data-testid="suppressed-list">
      <li v-for="item in report.suppressed" :key="`${item.level}-${item.region}-${item.label}`">
        {{ item.level === 'region' ? '区级' : '省级' }} · {{ item.region }} · {{ item.label }} —— {{ reasonLabel(item.reason) }}
      </li>
      <li v-if="!report.suppressed.length">无</li>
    </ul>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchJson } from '@/api/client'

type ReportRow = {
  level: 'region' | 'province'
  region: string
  label: string
  order_count: number
  amount: number
}

type Report = {
  rows: ReportRow[]
  totals: { order_count: number; amount: number }
  included_regions: string[]
  included_provinces: string[]
  pending_confirmations: string[]
  suppressed: { level: string; region: string; label: string; reason: string }[]
}

type DecisionProvince = {
  region: string
  province: string
  eligible: boolean
  allowed: boolean
  reason: string
  order_count?: number
  amount?: number
}

type DecisionRegion = {
  region: string
  eligible: boolean
  rollup_allowed: boolean
  conflict: boolean
  reason: string
  order_count?: number
  amount?: number
}

type ScenarioResponse = {
  decision: {
    provinces: DecisionProvince[]
    regions: DecisionRegion[]
  }
  report: Report
}

const provinceMin = ref(5)
const regionMin = ref(5)
const includeMetrics = ref(false)
const report = ref<Report>({
  rows: [],
  totals: { order_count: 0, amount: 0 },
  included_regions: [],
  included_provinces: [],
  pending_confirmations: [],
  suppressed: [],
})
const provinces = ref<DecisionProvince[]>([])
const regions = ref<DecisionRegion[]>([])
const errorMessage = ref('')

const cards = computed(() => [
  { label: '发布报告合计订单', value: report.value.totals.order_count },
  { label: '发布报告合计金额', value: report.value.totals.amount },
  { label: '待负责人确认区域', value: report.value.pending_confirmations.length },
  { label: '未披露项', value: report.value.suppressed.length },
])

const decisionRows = computed(() => [
  ...regions.value.map((row) => ({
    level: 'region',
    name: row.region,
    region: row.region,
    eligible: row.eligible,
    allowed: row.rollup_allowed,
    reason: row.reason,
    order_count: row.order_count,
  })),
  ...provinces.value.map((row) => ({
    level: 'province',
    name: row.province,
    region: row.region,
    eligible: row.eligible,
    allowed: row.allowed,
    reason: row.reason,
    order_count: row.order_count,
  })),
])

const REASON_LABELS: Record<string, string> = {
  allowed: '已达门槛并披露',
  below_province_threshold: '省份有效订单未达门槛',
  below_region_threshold: '区域有效订单未达门槛',
  province_not_approved: '省份未获批准',
  province_detail_preferred: '省份明细优先，不做区级汇总',
  owner_confirmation_pending: '省与区资格冲突，待负责人确认',
  region_rollup_approved: '负责人已批准区级汇总',
  rolled_up_into_region: '已并入区级汇总',
}

function reasonLabel(reason: string): string {
  return REASON_LABELS[reason] ?? reason
}

async function reload() {
  errorMessage.value = ''
  const params = new URLSearchParams({
    province_min_orders: String(provinceMin.value || 5),
    region_min_orders: String(regionMin.value || 5),
  })
  if (includeMetrics.value) params.set('include_metrics', 'true')
  try {
    const payload = await fetchJson<ScenarioResponse>(
      `/api/region-publication/scenario?${params.toString()}`,
    )
    report.value = payload.report
    provinces.value = payload.decision.provinces
    regions.value = payload.decision.regions
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '地区发布核定失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.checkbox { flex-direction: row; align-items: center; gap: 6px; }
.section-title { font-size: 14px; margin: 18px 0 8px; }
.suppressed-list { background: #fff; border: 1px solid var(--border); border-radius: 8px; margin: 0; padding: 10px 24px; font-size: 13px; color: var(--muted); }
</style>
