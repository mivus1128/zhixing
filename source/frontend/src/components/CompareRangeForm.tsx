import { useState, type FormEvent } from 'react'
import type { CompareDateRange } from '../api/types.compare'

interface CompareRangeFormProps {
  range: CompareDateRange
  loading: boolean
  onApply: (range: CompareDateRange) => void
}

export function CompareRangeForm({
  range,
  loading,
  onApply,
}: CompareRangeFormProps) {
  const [draft, setDraft] = useState(range)
  const [error, setError] = useState<string | null>(null)

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()

    if (!draft.from || !draft.to) {
      setError('起止日期都要填写。')
      return
    }
    if (draft.from > draft.to) {
      setError('起始日期不能晚于结束日期。')
      return
    }

    setError(null)
    onApply(draft)
  }

  return (
    <form className="compare-range" onSubmit={handleSubmit}>
      <div className="compare-range-copy">
        <strong>选择验证区间</strong>
        <small>请求只提交 from / to；后端按 context_digest 配对。</small>
      </div>
      <label>
        <span>从</span>
        <input
          type="date"
          value={draft.from}
          max={draft.to || undefined}
          onChange={(event) => setDraft((value) => ({ ...value, from: event.target.value }))}
        />
      </label>
      <span className="compare-range-arrow" aria-hidden="true">→</span>
      <label>
        <span>到</span>
        <input
          type="date"
          value={draft.to}
          min={draft.from || undefined}
          onChange={(event) => setDraft((value) => ({ ...value, to: event.target.value }))}
        />
      </label>
      <button type="submit" disabled={loading}>
        {loading ? '正在比对' : '应用区间'}
      </button>
      {error && <p className="compare-range-error" role="alert">{error}</p>}
    </form>
  )
}
