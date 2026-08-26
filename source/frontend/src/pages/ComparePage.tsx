// 验证期临时脚手架：删除本页时，必须同时删除 /api/runs/compare 与全部 Compare* 文件。
import { useCallback, useMemo, useState } from 'react'
import { api } from '../api'
import type { CompareDateRange } from '../api/types.compare'
import { CompareRangeForm } from '../components/CompareRangeForm'
import { CompareResults } from '../components/CompareResults'
import { CompareSummary } from '../components/CompareSummary'
import { PageHeader } from '../components/PageHeader'
import { ResourceMessage } from '../components/ResourceMessage'
import { useApiResource } from '../hooks/useApiResource'
import '../styles/compare.css'

function toDateInputValue(date: Date): string {
  const localDate = new Date(date.getTime() - date.getTimezoneOffset() * 60_000)
  return localDate.toISOString().slice(0, 10)
}

function initialRange(): CompareDateRange {
  const to = new Date()
  const from = new Date(to)
  from.setDate(from.getDate() - 6)
  return { from: toDateInputValue(from), to: toDateInputValue(to) }
}

function toApiRange(range: CompareDateRange) {
  return {
    from: `${range.from}T00:00:00+08:00`,
    to: `${range.to}T23:59:59.999+08:00`,
  }
}

export function ComparePage() {
  const defaultRange = useMemo(initialRange, [])
  const [range, setRange] = useState(defaultRange)
  const loadComparison = useCallback(
    () => api.compareRuns(toApiRange(range)),
    [range],
  )
  const comparison = useApiResource(loadComparison)

  function handleApplyRange(nextRange: CompareDateRange) {
    if (nextRange.from === range.from && nextRange.to === range.to) {
      comparison.reload()
      return
    }
    setRange(nextRange)
  }

  return (
    <section className="compare-page page-enter">
      <PageHeader
        title="验证对比"
        description="比较两套系统在相同输入下的判断结果。"
        aside="验证期功能"
      />

      <div className="compare-temporary-note" role="note">
        <span>V</span>
        <p><strong>上线即删除。</strong> 本页与 <code>/api/runs/compare</code> 同生同灭，不进入长期产品结构。</p>
      </div>

      <CompareRangeForm
        range={range}
        loading={comparison.status === 'loading'}
        onApply={handleApplyRange}
      />

      {comparison.status === 'loading' && (
        <ResourceMessage
          kind="loading"
          title="正在核对两代判断"
          message="只比较同一 context_digest 下的输出，不跨输入强行配对。"
        />
      )}
      {comparison.status === 'error' && (
        <ResourceMessage
          kind="error"
          title="对比数据读取失败"
          message={comparison.error}
          apiError={comparison.apiError}
          onRetry={comparison.reload}
        />
      )}
      {comparison.status === 'success' && (
        <>
          <CompareSummary
            summary={comparison.data.汇总}
            items={comparison.data.对比项}
          />
          {comparison.data.对比项.length === 0 ? (
            <ResourceMessage
              kind="empty"
              title="区间内零对比项"
              message="请求成功，但没有可比较的判断。请调整区间，或检查两套系统是否产出了相同 context_digest 的归档。"
            />
          ) : (
            <CompareResults items={comparison.data.对比项} />
          )}
        </>
      )}
    </section>
  )
}
