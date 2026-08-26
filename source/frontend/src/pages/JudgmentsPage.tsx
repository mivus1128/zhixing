import { useCallback, useMemo, useState } from 'react'
import { api, getJudgmentHistory } from '../api'
import {
  JudgmentDetailDrawer,
  JudgmentRow,
  type JudgmentRecord,
} from '../components/JudgmentRow'
import { PageHeader } from '../components/PageHeader'
import { ResourceMessage } from '../components/ResourceMessage'
import { useApiResource } from '../hooks/useApiResource'

interface MonthCursor {
  year: number
  month: number
}

function monthRange(cursor: MonthCursor) {
  const month = String(cursor.month + 1).padStart(2, '0')
  const lastDay = new Date(cursor.year, cursor.month + 1, 0).getDate()
  return {
    from: `${cursor.year}-${month}-01T00:00:00+08:00`,
    to: `${cursor.year}-${month}-${String(lastDay).padStart(2, '0')}T23:59:59.999+08:00`,
  }
}

function moveMonth(cursor: MonthCursor, delta: number): MonthCursor {
  const date = new Date(cursor.year, cursor.month + delta, 1)
  return { year: date.getFullYear(), month: date.getMonth() }
}

function monthKey(cursor: MonthCursor): number {
  return cursor.year * 12 + cursor.month
}

export function JudgmentsPage() {
  const now = useMemo(() => new Date(), [])
  const currentMonth = useMemo<MonthCursor>(
    () => ({ year: now.getFullYear(), month: now.getMonth() }),
    [now],
  )
  const [cursor, setCursor] = useState<MonthCursor>(currentMonth)
  const [selectedRecord, setSelectedRecord] = useState<JudgmentRecord | null>(null)
  const range = useMemo(() => monthRange(cursor), [cursor])
  const loadHistory = useCallback(() => getJudgmentHistory(range), [range])
  const history = useApiResource(loadHistory)
  const objects = useApiResource(api.getObjects)
  const monthLabel = `${cursor.year} 年 ${cursor.month + 1} 月`
  const nextDisabled = monthKey(cursor) >= monthKey(currentMonth)

  const objectTypes = useMemo(
    () => new Map(
      objects.status === 'success'
        ? objects.data.map((object) => [object.object_id, object.类型] as const)
        : [],
    ),
    [objects],
  )

  const records = useMemo<JudgmentRecord[]>(() => {
    if (history.status !== 'success') {
      return []
    }

    return history.data.flatMap((run) => {
      const judgments = run.交易对象判断.map((judgment, index) => {
        const instructions = run.待执行指令.filter(
          (instruction) => `${instruction.market}_${instruction.symbol}` === judgment.object_id,
        )
        return {
          kind: 'judgment' as const,
          key: `${run.strategy_id}:${judgment.object_id}:${index}`,
          run,
          judgment,
          instructions,
          objectType: objectTypes.get(judgment.object_id),
        }
      })
      const issues = (run.本轮问题 ?? []).map((issue, index) => ({
        kind: 'issue' as const,
        key: `${run.strategy_id}:issue:${issue.object_id ?? 'run'}:${index}`,
        run,
        issue,
      }))
      return [...judgments, ...issues]
    })
  }, [history, objectTypes])

  function showMonth(delta: number) {
    setSelectedRecord(null)
    setCursor((value) => moveMonth(value, delta))
  }

  return (
    <section className="judgments-page page-enter">
      <PageHeader
        title="判断记录"
        description="按时间查看模型判断、关联指令与执行结果。"
        aside="按月查询"
      />

      <div className="history-toolbar" aria-label="历史月份导航">
        <button type="button" onClick={() => showMonth(-1)}>
          ← 更早一月
        </button>
        <div>
          <span>正在查看</span>
          <strong>{monthLabel}</strong>
        </div>
        <button
          type="button"
          disabled={nextDisabled}
          onClick={() => showMonth(1)}
        >
          后一月 →
        </button>
      </div>

      {history.status === 'loading' && (
        <ResourceMessage
          kind="loading"
          title="正在读取判断历史"
          message="本月判断、指令与用量通过一个归档列表请求读取，不加载完整 context。"
        />
      )}
      {history.status === 'error' && (
        <ResourceMessage
          kind="error"
          title="判断历史读取失败"
          message={history.error}
          apiError={history.apiError}
          onRetry={history.reload}
        />
      )}
      {history.status === 'success' && records.length === 0 && (
        <ResourceMessage
          kind="empty"
          title={`${monthLabel}没有判断或未产出记录`}
          message="该月份成功返回空数组；可继续向前翻阅，不代表系统当前故障。"
        />
      )}
      {history.status === 'success' && records.length > 0 && (
        <section className="judgment-ledger" aria-label={`${monthLabel}判断列表`}>
          <header className="judgment-ledger-meta">
            <span>{history.data.length} 轮</span>
            <strong>{records.length} 条记录</strong>
            {objects.status === 'error' && <small>标的类型读取失败，不影响判断原文</small>}
          </header>
          <div className="judgment-columns" aria-hidden="true">
            <span>时间</span>
            <span>标的</span>
            <span>操作</span>
            <span>置信度</span>
            <span>指令</span>
            <span>结果</span>
            <span />
          </div>
          {records.map((record) => (
            <JudgmentRow
              key={record.key}
              record={record}
              active={selectedRecord?.key === record.key}
              onOpen={() => setSelectedRecord(record)}
            />
          ))}
        </section>
      )}

      {selectedRecord && (
        <JudgmentDetailDrawer
          key={selectedRecord.key}
          record={selectedRecord}
          onClose={() => setSelectedRecord(null)}
        />
      )}
    </section>
  )
}
