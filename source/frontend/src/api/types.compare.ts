import type {
  CompareRunsParams,
  ComparisonDecision,
  ComparisonItem,
  RunComparison,
} from './types'

export type CompareDecision = ComparisonDecision
export type CompareItem = ComparisonItem
export type CompareData = RunComparison
export type CompareItemKind = 'matched' | 'mismatched' | 'one-sided'
export type CompareDateRange = Required<Pick<CompareRunsParams, 'from' | 'to'>>

export function getCompareItemKind(item: CompareItem): CompareItemKind {
  if (!item.tradepilot || !item.zhixing) {
    return 'one-sided'
  }
  return item.一致 ? 'matched' : 'mismatched'
}
