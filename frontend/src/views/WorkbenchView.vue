<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button
              v-if="isRecoveryModule"
              class="link"
              type="button"
              :data-testid="`inspect-${row.id}`"
              @click="inspectRecovery(row)"
            >
              {{ meta.key === 'failure_quarantine' ? '处置交接单' : '恢复回执' }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <section v-if="isRecoveryModule" class="recovery-panel" data-testid="recovery-panel">
      <header class="recovery-head">
        <h3>{{ meta.key === 'failure_quarantine' ? '处置交接单' : '恢复回执数据' }}</h3>
        <span v-if="inspectedFingerprint" class="recovery-fp">隔离指纹：{{ inspectedFingerprint }}</span>
      </header>

      <div v-if="meta.key === 'match_review'" class="recovery-actions">
        <span v-if="claimFeedback" class="recovery-feedback" data-testid="claim-feedback">{{ claimFeedback }}</span>
        <button class="btn" type="button" data-testid="claim-queue" @click="claimFromQueue" :disabled="!selectedReview">
          认领所选复核
        </button>
        <button class="btn ghost" type="button" data-testid="withdraw-review" @click="withdrawSelected" :disabled="!selectedReview">
          人工退出
        </button>
      </div>

      <article v-if="handover" class="recovery-card" data-testid="handover-card">
        <p><strong>认领人：</strong>{{ handover.claimant }}（{{ handover.claimed_by || '无处置方' }}）</p>
        <p><strong>剩余动作：</strong>{{ handover.remaining_actions.length ? handover.remaining_actions.join('、') : '已办结，无剩余动作' }}</p>
        <p><strong>实际生效恢复结果：</strong>{{ handover.final_status }}</p>
        <p><strong>当次失败数据：</strong>{{ handover.last_failure?.detail || '无' }}</p>
      </article>

      <article v-if="recovery" class="recovery-card" data-testid="recovery-card">
        <p><strong>处置状态：</strong>{{ recovery.state }}｜<strong>最终处置状态：</strong>{{ recovery.final_status || '处理中' }}</p>
        <p><strong>处置资格：</strong>{{ recovery.eligibility_owner || '未占用' }}（{{ recovery.holder || '—' }}）</p>
        <p><strong>能否回到自动恢复：</strong>{{ recovery.auto_recovery_available ? '可以' : '不可以' }}</p>
        <p><strong>恢复回执：</strong></p>
        <ul class="receipt-list">
          <li v-for="receipt in recovery.receipts" :key="receipt.id" :data-testid="`receipt-${receipt.id}`">
            #{{ receipt.id }} {{ receipt.ok ? '成功' : '失败' }} · {{ receipt.detail }}
            <span v-if="receipt.effective">（实际生效）</span>
          </li>
          <li v-if="!recovery.receipts.length" class="empty-state">暂无回执</li>
        </ul>
        <p><strong>恢复产物：</strong>
          <span v-if="recovery.artifact" :data-testid="`artifact-${recovery.artifact.id}`">
            #{{ recovery.artifact.id }} · {{ recovery.artifact.kind }} ·
            {{ recovery.artifact.handed_back ? '已交回样本' : '待交回' }}
          </span>
          <span v-else>暂无有效产物</span>
        </p>
      </article>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  claimReview,
  createEntry,
  downloadEntries,
  listEntries,
  loadHandover,
  loadRecovery,
  loadReviewQueue,
  moduleMeta,
  runAction as applyAction,
  withdrawReview,
} from '@/api/backend-service'
import type { EntryRow, HandoverResult, RecoverySnapshot, ReviewQueueItem } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const isRecoveryModule = computed(
  () => meta.key === 'failure_quarantine' || meta.key === 'match_review',
)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const handover = ref<HandoverResult | null>(null)
const recovery = ref<RecoverySnapshot | null>(null)
const inspectedFingerprint = ref('')
const queue = ref<ReviewQueueItem[]>([])
const selectedReview = ref<ReviewQueueItem | null>(null)
const claimFeedback = ref('')
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

function rowFingerprint(row: EntryRow): string {
  return String(row['来源指纹'] ?? row['样本指纹'] ?? row['匹配记录'] ?? '')
}

async function inspectRecovery(row: EntryRow) {
  errorMessage.value = ''
  handover.value = null
  recovery.value = null
  try {
    if (meta.key === 'failure_quarantine') {
      handover.value = await loadHandover(Number(row.id))
      inspectedFingerprint.value = handover.value.fingerprint
    } else {
      const fingerprint = rowFingerprint(row)
      inspectedFingerprint.value = fingerprint
      if (fingerprint) recovery.value = await loadRecovery(fingerprint)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '恢复回执读取失败'
  }
}

async function claimFromQueue() {
  const item = selectedReview.value
  if (!item) return
  claimFeedback.value = ''
  try {
    const result = await claimReview(item.id, '数据治理专员', item.fingerprint)
    claimFeedback.value = result.message
    inspectedFingerprint.value = item.fingerprint
    recovery.value = await loadRecovery(item.fingerprint)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '认领失败'
  }
}

async function withdrawSelected() {
  const item = selectedReview.value
  if (!item) return
  claimFeedback.value = ''
  try {
    const result = await withdrawReview(item.id)
    claimFeedback.value = String(result.message)
    if (item.fingerprint) recovery.value = await loadRecovery(item.fingerprint)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '人工退出失败'
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    if (meta.key === 'match_review') {
      selectedReview.value = queue.value.find((item) => item.id === Number(row.id)) ?? {
        record_number: String(row['记录编号'] ?? ''),
        id: Number(row.id),
        fingerprint: rowFingerprint(row),
        claimant: String(row['认领人'] ?? row['复核角色'] ?? ''),
        remaining_actions: [],
        claimable: true,
        recovery: null,
      }
    }
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
    if (isRecoveryModule.value) await inspectRecovery(row)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
    if (meta.key === 'match_review') {
      queue.value = (await loadReviewQueue()).items
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>
