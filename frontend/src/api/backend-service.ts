import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type {
  ActionResult,
  EntryRow,
  HandoverResult,
  OverviewResult,
  PageResult,
  RecoverySnapshot,
  ReviewQueueResult,
} from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

export async function loadHandover(entryId: number): Promise<HandoverResult> {
  const response = await request(`/api/failure_quarantine/${entryId}/handover`)
  if (!response.ok) throw new Error('处置交接单读取失败')
  return await response.json()
}

export async function loadRecovery(fingerprint: string): Promise<RecoverySnapshot> {
  const response = await request(`/api/recovery/${encodeURIComponent(fingerprint)}`)
  if (!response.ok) throw new Error('恢复回执数据读取失败')
  return await response.json()
}

export async function loadReviewQueue(): Promise<ReviewQueueResult> {
  const response = await request('/api/match_review/queue')
  if (!response.ok) throw new Error('待复核队列读取失败')
  return await response.json()
}

export async function claimReview(
  entryId: number,
  role: string,
  fingerprint: string,
): Promise<ActionResult & Record<string, unknown>> {
  const response = await request(`/api/match_review/${entryId}/claim`, {
    method: 'POST',
    body: JSON.stringify({ role, fingerprint }),
  })
  return await response.json()
}

export async function withdrawReview(
  entryId: number,
): Promise<ActionResult & Record<string, unknown>> {
  const response = await request(`/api/match_review/${entryId}/withdraw`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
  return await response.json()
}

