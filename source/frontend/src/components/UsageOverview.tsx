import {
  useCallback,
  useId,
  useState,
  type CSSProperties,
  type FormEvent,
} from 'react'
import {
  api,
  type UsageGroupBy,
  type UsageQuery,
  type UsageRow,
} from '../api'
import { useApiResource } from '../hooks/useApiResource'
import { formatNumber } from '../lib/format'
import { ResourceMessage } from './ResourceMessage'

export const CACHE_WARNING_THRESHOLD = 0.5
export const CACHE_CRITICAL_THRESHOLD = 0.1

const groupLabels: Record<UsageGroupBy, string> = {
  day: '按日',
  object: '按标的',
  model: '按模型',
}

function formatHitRate(value: number): string {
  return `${(value * 100).toLocaleString('zh-CN', {
    minimumFractionDigits: value < 0.1 ? 1 : 0,
    maximumFractionDigits: 1,
  })}%`
}

function usageRowLabel(row: UsageRow, groupBy: UsageGroupBy): string {
  if (groupBy === 'day' && '日期' in row) {
    return row.日期
  }
  if (groupBy === 'object' && 'object_id' in row) {
    return row.object_id
  }
  if (groupBy === 'model' && 'model' in row) {
    return row.model
  }
  return '分组字段缺失'
}

function usageRowGroup(row: UsageRow): UsageGroupBy {
  if ('日期' in row) {
    return 'day'
  }
  if ('object_id' in row) {
    return 'object'
  }
  return 'model'
}

function cacheTone(rate: number | null): 'healthy' | 'warning' | 'critical' | 'unknown' {
  if (rate === null) {
    return 'unknown'
  }
  if (rate < CACHE_CRITICAL_THRESHOLD) {
    return 'critical'
  }
  if (rate < CACHE_WARNING_THRESHOLD) {
    return 'warning'
  }
  return 'healthy'
}

function CacheHealth({ rows }: { rows: UsageRow[] }) {
  const inputTokens = rows.reduce((total, row) => total + row.input_tokens, 0)
  const cachedTokens = rows.reduce((total, row) => total + row.cached_tokens, 0)
  const rate = inputTokens > 0 ? cachedTokens / inputTokens : null
  const tone = cacheTone(rate)
  const message =
    tone === 'critical'
      ? '低于 10%，请优先排查前缀缓存。'
      : tone === 'warning'
        ? '低于 50%，系统不会报错，但账单可能明显升高。'
        : tone === 'healthy'
          ? '达到 50% 以上，缓存复用处于正常区间。'
          : '区间内没有输入 token，暂时无法计算。'

  return (
    <div
      className={`usage-cache-health is-${tone}`}
      role={tone === 'warning' || tone === 'critical' ? 'alert' : 'status'}
    >
      <div className="usage-cache-copy">
        <span>区间缓存命中率</span>
        <strong>{rate === null ? '—' : formatHitRate(rate)}</strong>
      </div>
      <div className="usage-cache-diagnostic">
        <strong>
          {tone === 'critical'
            ? '严重偏低'
            : tone === 'warning'
              ? '需要关注'
              : tone === 'healthy'
                ? '缓存正常'
                : '无法计算'}
        </strong>
        <p>{message}</p>
        <div className="usage-cache-track" aria-hidden="true">
          <span
            style={{
              '--cache-rate': `${Math.min(100, Math.max(0, (rate ?? 0) * 100))}%`,
            } as CSSProperties}
          />
        </div>
      </div>
      <dl className="usage-cache-totals">
        <div>
          <dt>输入 token</dt>
          <dd>{formatNumber(inputTokens)}</dd>
        </div>
        <div>
          <dt>命中 token</dt>
          <dd>{formatNumber(cachedTokens)}</dd>
        </div>
      </dl>
    </div>
  )
}

function UsageTable({ rows, groupBy }: { rows: UsageRow[]; groupBy: UsageGroupBy }) {
  return (
    <div className="usage-table-wrap">
      <table className="usage-ledger" role="table">
        <thead role="rowgroup">
          <tr role="row">
            <th scope="col" role="columnheader">{groupLabels[groupBy]}</th>
            <th scope="col" role="columnheader">轮数</th>
            <th scope="col" role="columnheader">输入 token</th>
            <th scope="col" role="columnheader">输出 token</th>
            <th scope="col" role="columnheader">推理 token</th>
            <th scope="col" role="columnheader">命中 token</th>
            <th scope="col" role="columnheader">缓存命中率</th>
          </tr>
        </thead>
        <tbody role="rowgroup">
          {rows.map((row) => {
            const label = usageRowLabel(row, groupBy)
            const tone = cacheTone(row.缓存命中率)
            return (
              <tr role="row" key={label}>
                <th scope="row" role="rowheader">{label}</th>
                <td data-label="轮数" role="cell">{formatNumber(row.轮数)}</td>
                <td data-label="输入 token" role="cell">{formatNumber(row.input_tokens)}</td>
                <td data-label="输出 token" role="cell">{formatNumber(row.output_tokens)}</td>
                <td data-label="推理 token" role="cell">{formatNumber(row.reasoning_tokens)}</td>
                <td data-label="命中 token" role="cell">{formatNumber(row.cached_tokens)}</td>
                <td data-label="缓存命中率" role="cell">
                  <span className={`usage-hit-rate is-${tone}`}>
                    {formatHitRate(row.缓存命中率)}
                    {tone === 'warning' || tone === 'critical' ? ' · 低于 50%' : ''}
                  </span>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export function UsageOverview() {
  const fieldId = useId()
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [groupBy, setGroupBy] = useState<UsageGroupBy>('day')
  const [query, setQuery] = useState<UsageQuery>({ group_by: 'day' })
  const [filterError, setFilterError] = useState<string | null>(null)
  const loadUsage = useCallback(() => api.getUsage(query), [query])
  const usage = useApiResource(loadUsage)
  const displayedGroupBy =
    usage.status === 'success' && usage.data[0]
      ? usageRowGroup(usage.data[0])
      : query.group_by

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (from && to && from > to) {
      setFilterError('开始日期不能晚于结束日期。')
      return
    }

    const nextQuery: UsageQuery = { group_by: groupBy }
    if (from) {
      nextQuery.from = from
    }
    if (to) {
      nextQuery.to = to
    }
    setFilterError(null)
    setQuery(nextQuery)
  }

  return (
    <section className="ledger-section runtime-section" aria-labelledby="usage-title">
      <header className="section-heading">
        <div>
          <h2 id="usage-title">Token 用量</h2>
          <p>当前契约没有金额、币种或模型单价字段，前端只展示已定义的用量，不自行估算账单。</p>
        </div>
      </header>

      <form className="usage-toolbar" onSubmit={handleSubmit}>
        <label htmlFor={`${fieldId}-from`}>
          <span>开始日期</span>
          <input
            id={`${fieldId}-from`}
            type="date"
            value={from}
            onChange={(event) => setFrom(event.target.value)}
          />
        </label>
        <label htmlFor={`${fieldId}-to`}>
          <span>结束日期</span>
          <input
            id={`${fieldId}-to`}
            type="date"
            value={to}
            onChange={(event) => setTo(event.target.value)}
          />
        </label>
        <label htmlFor={`${fieldId}-group`}>
          <span>分组方式</span>
          <select
            id={`${fieldId}-group`}
            value={groupBy}
            onChange={(event) => setGroupBy(event.target.value as UsageGroupBy)}
          >
            <option value="day">按日</option>
            <option value="object">按标的</option>
            <option value="model">按模型</option>
          </select>
        </label>
        <button type="submit" className="primary-action">查询用量</button>
      </form>

      {filterError && <div className="runtime-inline-error" role="alert">{filterError}</div>}
      {usage.status === 'loading' && (
        <ResourceMessage kind="loading" title="正在聚合用量" message="读取 token 与缓存命中情况。" />
      )}
      {usage.status === 'error' && (
        <ResourceMessage
          kind="error"
          title="用量读取失败"
          message={usage.error}
          apiError={usage.apiError}
          onRetry={usage.reload}
        />
      )}
      {usage.status === 'success' && usage.data.length === 0 && (
        <ResourceMessage
          kind="empty"
          title="区间内没有用量数据"
          message="这不是 0% 命中率；请调整日期范围，或确认该区间是否产生过策略轮次。"
        />
      )}
      {usage.status === 'success' && usage.data.length > 0 && (
        <>
          <CacheHealth rows={usage.data} />
          <UsageTable rows={usage.data} groupBy={displayedGroupBy} />
        </>
      )}
    </section>
  )
}
