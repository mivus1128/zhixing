import type { ApiError } from './errors'

export type ApiSuccess<T> = {
  ok: true
  data: T
}

export type ApiFailure = {
  ok: false
  error: ApiError
}

export type ApiResponse<T> = ApiSuccess<T> | ApiFailure

export type SystemName = 'zhixing' | 'tradepilot'
export type Market = 'SH' | 'SZ'
export type AssetType = 'ETF' | '股票'
export type TradeObjectType = '交易标的' | '行情对象'
export type Operation = 'buy' | 'sell' | 'hold' | 'cancel'
export type InstructionAction = Exclude<Operation, 'hold'>
export type InstructionStatus =
  | 'pending'
  | 'confirmed'
  | 'submitted'
  | 'rejected'
  | 'expired'

export interface SystemStatus {
  system_name: 'zhixing'
  app_version: string
  运行模式: 'dry_run' | 'live'
  无人值守: boolean
  登录状态: '已登录' | '未登录' | '未知'
  最近采集时间: string | null
  最近策略时间: string | null
  数据源: string
  上一轮成功时间: string | null
  连续失败轮数: number
  最近失败原因: string | null
}

export interface Position {
  是否持仓: boolean
  持仓数量: number
  可用数量: number
  成本价: number
  最新价: number
  市值: number
  浮动盈亏: number
}

export interface TradeObject {
  object_id: string
  market: Market
  symbol: string
  名称: string
  类型: TradeObjectType
  资产类型: AssetType
  交易单位: number
  持仓: Position | null
  最新切片时间: string | null
  是否当日行情: boolean
}

export type TradeObjectDraft = Pick<
  TradeObject,
  'market' | 'symbol' | '名称' | '类型' | '资产类型'
>

export interface AccountSummary {
  采集时间: string
  账户标识: string
  总资产: number | null
  可用资金: number | null
  资金余额: number | null
  冻结资金: number | null
  证券市值: number | null
  持仓数量: number
  持仓列表: Record<string, unknown>[]
  说明?: string[]
}

export interface RiskControl {
  最大单笔风险: string
  仓位约束: string
  禁止执行条件: string[]
}

export interface JudgmentEvidence {
  起: string
  止: string
  行情: Record<string, unknown>[]
}

export interface ObjectJudgment {
  object_id: string
  名称: string
  操作: Operation
  理由: string[]
  风险: string[]
  置信度: number
  // 二代归档缺少这个字段，列表兼容读取时不把缺失误作“无改判条件”。
  改判条件?: string
  依据数据: JudgmentEvidence
}

export interface RejectionReason {
  code: string
  message: string
}

interface InstructionFields {
  instruction_code: string
  market: Market
  symbol: string
  name: string
  理由: string
  风险提示: string
}

type InstructionOutcome =
  | {
      状态: 'rejected'
      拦截原因: [RejectionReason, ...RejectionReason[]]
    }
  | {
      状态: Exclude<InstructionStatus, 'rejected'>
      拦截原因: []
    }

export type BuyOrSellInstruction = InstructionFields & InstructionOutcome & {
  action: 'buy' | 'sell'
  qty: number
  limit_price: number
  wtbh: null
}

export type CancelInstruction = InstructionFields & InstructionOutcome & {
  action: 'cancel'
  qty: null
  limit_price: null
  wtbh: string
}

export type Instruction = BuyOrSellInstruction | CancelInstruction

export interface ModelUsage {
  object_id: string
  input_tokens: number
  output_tokens: number
  reasoning_tokens: number
  cached_tokens: number
}

export interface RunIssue {
  object_id: string | null
  code: string
  message: string
}

export interface RunRecord {
  strategy_id: string
  system_name: SystemName
  app_version: string
  生成时间: string
  总体判断: string
  风险控制: RiskControl
  交易对象判断: ObjectJudgment[]
  待执行指令: Instruction[]
  model: string
  llm_provider: string
  model_usage: ModelUsage[]
  // 二代归档没有该字段；知行归档正常时仍会显式返回空数组。
  本轮问题?: RunIssue[]
  data_window: {
    起: string
    止: string
  }
  context_digest: string
}

export interface StrategyRun extends RunRecord {
  context: Record<string, unknown>
}

export type RunSummary = Omit<StrategyRun, 'context'> & {
  判断条数: number
  指令条数: number
}

export interface ComparisonDecision {
  操作: Operation
  置信度: number
}

export interface ComparisonItem {
  context_digest: string
  生成时间: string
  object_id: string
  名称: string
  tradepilot?: ComparisonDecision
  zhixing?: ComparisonDecision
  一致: boolean
}

export interface RunComparison {
  对比项: ComparisonItem[]
  汇总: {
    总条数: number
    一致条数: number
    一致率: number
  }
}

export interface RunListParams {
  limit?: number
  from?: string
  to?: string
  system_name?: SystemName
}

export interface CompareRunsParams {
  from?: string
  to?: string
}
