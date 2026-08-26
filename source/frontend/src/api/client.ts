import type {
  AccountSummary,
  ApiResponse,
  CompareRunsParams,
  Instruction,
  RunComparison,
  RunListParams,
  RunSummary,
  StrategyRun,
  SystemStatus,
  TradeObject,
  TradeObjectDraft,
} from './types'
import type { RuntimeApi } from './types.runtime.ts'

export interface ApiClient extends RuntimeApi {
  getStatus(): Promise<ApiResponse<SystemStatus>>
  getObjects(): Promise<ApiResponse<TradeObject[]>>
  createObject(draft: TradeObjectDraft): Promise<ApiResponse<unknown>>
  updateObject(objectId: string, draft: TradeObjectDraft): Promise<ApiResponse<unknown>>
  deleteObject(objectId: string): Promise<ApiResponse<unknown>>
  getAccount(): Promise<ApiResponse<AccountSummary>>
  getRuns(params?: RunListParams): Promise<ApiResponse<RunSummary[]>>
  getRun(strategyId: string): Promise<ApiResponse<StrategyRun>>
  compareRuns(params?: CompareRunsParams): Promise<ApiResponse<RunComparison>>
  getPendingInstructions(): Promise<ApiResponse<Instruction[]>>
  confirmInstruction(code: string): Promise<ApiResponse<never>>
}
