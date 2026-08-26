import { fixtureClient } from './fixtureClient'
import { createHttpClient } from './httpClient'
import { loadJudgmentHistory } from './history'
import type { JudgmentHistoryRange } from './history'

export { apiErrorMessages, apiErrorPresentation } from './errors'
export type { ApiError, ApiErrorPresentation, ApiProblem } from './errors'

const configuredApiBaseUrl = import.meta.env.VITE_API_BASE_URL?.trim()

// Local development stays deterministic; every production build talks to the
// same-origin /api gateway unless an explicit API base URL is supplied.
export const api = import.meta.env.DEV && !configuredApiBaseUrl
  ? fixtureClient
  : createHttpClient(configuredApiBaseUrl ?? '')

export function getJudgmentHistory(range: JudgmentHistoryRange) {
  return loadJudgmentHistory(api, range)
}

export type { ApiClient } from './client'
export type { JudgmentHistoryRange } from './history'
export type * from './types'
export type * from './types.runtime'
