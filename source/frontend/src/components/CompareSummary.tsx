import {
  getCompareItemKind,
  type CompareData,
  type CompareItem,
} from '../api/types.compare'

function formatRate(value: number): string {
  return `${(value * 100).toLocaleString('zh-CN', {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  })}%`
}

export function CompareSummary({
  summary,
  items,
}: {
  summary: CompareData['汇总']
  items: CompareItem[]
}) {
  const oneSidedCount = items.filter((item) => getCompareItemKind(item) === 'one-sided').length
  const mismatchCount = items.filter((item) => getCompareItemKind(item) === 'mismatched').length
  const hasItems = summary.总条数 > 0

  return (
    <section className="compare-summary" aria-labelledby="compare-rate-title">
      <div className="compare-rate">
        <span id="compare-rate-title">一致率</span>
        <strong>{hasItems ? formatRate(summary.一致率) : '—'}</strong>
        <small>
          {hasItems
            ? `${summary.一致条数} / ${summary.总条数} 条判断一致`
            : '区间内没有可比判断'}
        </small>
      </div>

      <div className="compare-summary-status">
        <span className={hasItems && mismatchCount + oneSidedCount === 0 ? 'is-clear' : 'is-alert'}>
          {hasItems
            ? mismatchCount + oneSidedCount === 0
              ? '本区间全一致'
              : '本区间需要核查'
            : '等待可比数据'}
        </span>
        <p>只把 context_digest 相同的输入视为同一轮；数据缺侧不算作判断分歧。</p>
      </div>

      <dl className="compare-counts">
        <div>
          <dt>总条数</dt>
          <dd>{summary.总条数}</dd>
        </div>
        <div>
          <dt>一致</dt>
          <dd>{summary.一致条数}</dd>
        </div>
        <div className="is-difference">
          <dt>判断分歧</dt>
          <dd>{mismatchCount}</dd>
        </div>
        <div className="is-missing">
          <dt>数据缺侧</dt>
          <dd>{oneSidedCount}</dd>
        </div>
      </dl>
    </section>
  )
}
