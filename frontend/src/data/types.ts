/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  [field: string]: string | number | boolean
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

export type RecoveryReceipt = {
  id: number
  fingerprint: string
  ok: boolean
  attempt: number | null
  detail: string
  source: string
  kind: string
  received_at: string
  effective: boolean
}

export type RecoveryArtifact = {
  id: number
  fingerprint: string
  owner: string
  holder: string
  kind: string
  payload: Record<string, unknown>
  created_at: string
  valid: boolean
  handed_back: boolean
}

export type DisposalAttempt = {
  id: number
  fingerprint: string
  kind: string
  owner: string | null
  holder: string
  acquired: boolean
  reason: string
  at: string
}

export type RecoverySnapshot = {
  fingerprint: string
  state: string
  eligibility_owner: string | null
  holder: string
  review_id: number | null
  quarantine_id: number | null
  artifact_id: number | null
  final_status: string
  message: string
  auto_recovery_available: boolean
  manual_continue_available: boolean
  last_failure: { attempt: number | null; detail: string; source: string; at: string } | null
  artifact: RecoveryArtifact | null
  artifacts: RecoveryArtifact[]
  receipts: RecoveryReceipt[]
  attempts: DisposalAttempt[]
  effective_receipt: RecoveryReceipt | null
  remaining_actions?: string[]
}

export type HandoverResult = {
  ok: boolean
  message: string
  fingerprint: string
  quarantine_id: number
  quarantine_status: string
  claimant: string
  claimed_by: string | null
  review_id: number | null
  review_status: string | null
  disposal_state: string
  remaining_actions: string[]
  final_status: string
  auto_recovery_available: boolean
  last_failure: RecoverySnapshot['last_failure']
  artifact: RecoveryArtifact | null
  attempts: DisposalAttempt[]
}

export type ReviewQueueItem = {
  record_number: string
  id: number
  fingerprint: string
  claimant: string
  remaining_actions: string[]
  claimable: boolean
  recovery: RecoverySnapshot | null
}

export type ReviewQueueResult = {
  ok: boolean
  message: string
  count: number
  records: string[]
  items: ReviewQueueItem[]
}

