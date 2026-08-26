import type { ApiClient } from './client'
import type { ApiResponse, RunSummary } from './types'

export interface JudgmentHistoryRange {
  from: string
  to: string
}

export function loadJudgmentHistory(
  client: ApiClient,
  range: JudgmentHistoryRange,
): Promise<ApiResponse<RunSummary[]>> {
  return client.getRuns({
    from: range.from,
    to: range.to,
    system_name: 'zhixing',
  })
}
