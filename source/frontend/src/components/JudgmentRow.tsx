import { useEffect, useRef, useState } from 'react'
import {
  api,
  type Instruction,
  type ObjectJudgment,
  type RunIssue,
  type RunSummary,
  type StrategyRun,
  type TradeObject,
} from '../api'
import { formatDateTime, formatNumber, formatPercent, formatPrice, formatTime } from '../lib/format'
import { LazyJson } from './LazyJson'

export type JudgmentRecord =
  | {
      kind: 'judgment'
      key: string
      run: RunSummary
      judgment: ObjectJudgment
      instructions: Instruction[]
      objectType: TradeObject['类型'] | undefined
    }
  | {
      kind: 'issue'
      key: string
      run: RunSummary
      issue: RunIssue
    }

type DetailState =
  | { status: 'idle' | 'loading' }
  | { status: 'success'; data: StrategyRun }
  | { status: 'error'; message: string }

const operationLabels = {
  buy: '买入',
  sell: '卖出',
  hold: '保持观察',
  cancel: '撤单',
} as const

const statusLabels = {
  pending: '待执行',
  confirmed: '已确认',
  submitted: '已提交',
  rejected: '已拦',
  expired: '已过期',
} as const

function InstructionText({ instruction }: { instruction: Instruction }) {
  if (instruction.action === 'cancel') {
    return <>{operationLabels[instruction.action]} · 委托 {instruction.wtbh}</>
  }

  return (
    <>
      {operationLabels[instruction.action]} · {formatNumber(instruction.qty)} 股 @ {formatPrice(instruction.limit_price)}
    </>
  )
}

function EvidenceValue({ value }: { value: unknown }) {
  if (value === undefined) {
    return <span className="empty-mark">—</span>
  }
  if (value === null || ['string', 'number', 'boolean'].includes(typeof value)) {
    return <>{String(value)}</>
  }
  return <LazyJson label="查看值" value={value} />
}

function ContextEvidence({
  recordKey,
  run,
  judgment,
}: {
  recordKey: string
  run: RunSummary
  judgment: ObjectJudgment
}) {
  const evidence = judgment.依据数据
  const headingId = `evidence-${recordKey}`

  if (!evidence || !Array.isArray(evidence.行情)) {
    return (
      <section className="drawer-section context-evidence" aria-labelledby={headingId}>
        <div className="drawer-section-heading">
          <div>
            <span>判断输入</span>
            <h3 id={headingId}>依据数据</h3>
          </div>
          <code>{run.context_digest}</code>
        </div>
        <p className="drawer-data-unavailable">
          该轮归档未包含契约要求的依据数据，无法展示行情切片。
        </p>
      </section>
    )
  }

  const columns = Array.from(
    new Set(evidence.行情.flatMap((row) => Object.keys(row))),
  )

  return (
    <section className="drawer-section context-evidence" aria-labelledby={headingId}>
      <div className="drawer-section-heading">
        <div>
          <span>判断输入</span>
          <h3 id={headingId}>依据数据</h3>
        </div>
        <code>{run.context_digest}</code>
      </div>

      <dl className="drawer-data-window">
        <div><dt>数据区间</dt><dd>{evidence.起} 至 {evidence.止}</dd></div>
        <div><dt>行情记录</dt><dd>{evidence.行情.length} 条</dd></div>
      </dl>

      <p className="drawer-note">
        行情字段按接口实际返回内容展示，不推断尚未进入契约的字段含义。
      </p>

      {evidence.行情.length === 0 ? (
        <p className="muted-copy">这条判断没有返回行情记录。</p>
      ) : columns.length === 0 ? (
        <p className="muted-copy">
          返回了 {evidence.行情.length} 条行情记录，但记录中还没有可展示字段。
        </p>
      ) : (
        <div className="evidence-record-list">
          {evidence.行情.map((row, rowIndex) => (
            <article className="evidence-record" key={rowIndex}>
              <strong>行情 {String(rowIndex + 1).padStart(2, '0')}</strong>
              <dl>
                {columns.map((column) => (
                  <div key={column}>
                    <dt>{column}</dt>
                    <dd><EvidenceValue value={row[column]} /></dd>
                  </div>
                ))}
              </dl>
            </article>
          ))}
        </div>
      )}
    </section>
  )
}

function RawRunJson({ strategyId }: { strategyId: string }) {
  const [state, setState] = useState<DetailState>({ status: 'idle' })

  async function loadDetail() {
    setState({ status: 'loading' })
    try {
      const response = await api.getRun(strategyId)
      setState(
        response.ok
          ? { status: 'success', data: response.data }
          : { status: 'error', message: response.error.message },
      )
    } catch {
      setState({ status: 'error', message: '完整归档请求失败。' })
    }
  }

  return (
    <details
      className="json-disclosure raw-json drawer-raw-json"
      onToggle={(event) => {
        if (event.currentTarget.open && state.status === 'idle') {
          void loadDetail()
        }
      }}
    >
      <summary>原始 JSON</summary>
      {state.status === 'loading' && <p className="raw-json-state">正在按需读取完整归档与 context…</p>}
      {state.status === 'error' && (
        <div className="raw-json-state is-error" role="alert">
          <span>{state.message}</span>
          <button type="button" onClick={() => void loadDetail()}>重试</button>
        </div>
      )}
      {state.status === 'success' && <pre>{JSON.stringify(state.data, null, 2)}</pre>}
    </details>
  )
}

function DecisionRow({
  record,
  active,
  onOpen,
}: {
  record: Extract<JudgmentRecord, { kind: 'judgment' }>
  active: boolean
  onOpen: () => void
}) {
  const { run, judgment, instructions } = record
  const isMarketObject = record.objectType === '行情对象'

  return (
    <article className={`judgment-record${active ? ' is-active' : ''}`}>
      <button
        className="judgment-toggle"
        type="button"
        aria-haspopup="dialog"
        aria-expanded={active}
        aria-controls={active ? 'judgment-detail-drawer' : undefined}
        onClick={onOpen}
      >
        <time dateTime={run.生成时间} data-label="时间">
          <strong>{formatTime(run.生成时间)}</strong>
          <small>{formatDateTime(run.生成时间).split(' ')[0]}</small>
        </time>
        <span className="judgment-object" data-label="标的">
          <code>{judgment.object_id}</code>
          <strong>{judgment.名称}</strong>
          {isMarketObject && <small>行情参照 · 不可交易</small>}
        </span>
        <span className={`operation is-${judgment.操作}`} data-label="操作">
          <strong>{operationLabels[judgment.操作]}</strong>
        </span>
        <span className="confidence" data-label="置信度">{formatPercent(judgment.置信度)}</span>
        <span className="instruction-stack" data-label="指令">
          {isMarketObject ? (
            <span className="not-applicable">不生成指令</span>
          ) : instructions.length === 0 ? (
            <span className="empty-mark">—</span>
          ) : (
            instructions.map((instruction) => (
              <span key={instruction.instruction_code}><InstructionText instruction={instruction} /></span>
            ))
          )}
        </span>
        <span className="result-stack" data-label="结果">
          {isMarketObject || instructions.length === 0 ? (
            <span className="empty-mark">—</span>
          ) : (
            instructions.map((instruction) => (
              <span className={`instruction-status is-${instruction.状态}`} key={instruction.instruction_code}>
                {statusLabels[instruction.状态]}
                {instruction.状态 === 'rejected' && instruction.拦截原因.map((reason) => (
                  <small key={`${reason.code}-${reason.message}`}>{reason.message}</small>
                ))}
              </span>
            ))
          )}
        </span>
        <span className="expand-mark" aria-hidden="true">查看 →</span>
      </button>
    </article>
  )
}

function IssueRow({
  record,
  active,
  onOpen,
}: {
  record: Extract<JudgmentRecord, { kind: 'issue' }>
  active: boolean
  onOpen: () => void
}) {
  const { issue, run } = record
  const objectLabel = issue.object_id ?? '整轮'

  return (
    <article className={`judgment-record is-run-issue${active ? ' is-active' : ''}`}>
      <button
        className="judgment-toggle"
        type="button"
        aria-haspopup="dialog"
        aria-expanded={active}
        aria-controls={active ? 'judgment-detail-drawer' : undefined}
        onClick={onOpen}
      >
        <time dateTime={run.生成时间} data-label="时间">
          <strong>{formatTime(run.生成时间)}</strong>
          <small>{formatDateTime(run.生成时间).split(' ')[0]}</small>
        </time>
        <span className="judgment-object" data-label="标的">
          <code>{objectLabel}</code>
          <strong>{issue.object_id === null ? '整轮问题' : '本轮未产出判断'}</strong>
        </span>
        <span className="operation is-issue" data-label="操作">
          <strong>未产出</strong>
        </span>
        <span className="confidence empty-mark" data-label="置信度">—</span>
        <span className="instruction-stack" data-label="指令"><span className="empty-mark">—</span></span>
        <span className="result-stack" data-label="结果"><span className="empty-mark">—</span></span>
        <span className="expand-mark" aria-hidden="true">查看 →</span>
      </button>
    </article>
  )
}

function DecisionDetail({
  record,
}: {
  record: Extract<JudgmentRecord, { kind: 'judgment' }>
}) {
  const { run, judgment, instructions } = record

  return (
    <>
      <section className="drawer-section" aria-labelledby={`reasons-${record.key}`}>
        <div className="drawer-section-heading">
          <div>
            <span>模型判断</span>
            <h3 id={`reasons-${record.key}`}>理由</h3>
          </div>
          <strong>{judgment.理由.length}</strong>
        </div>
        <ol className="drawer-numbered-list">
          {judgment.理由.map((reason, index) => <li key={`${index}-${reason}`}>{reason}</li>)}
        </ol>
      </section>

      <section className="drawer-section" aria-labelledby={`risks-${record.key}`}>
        <div className="drawer-section-heading">
          <div>
            <span>不确定性</span>
            <h3 id={`risks-${record.key}`}>风险</h3>
          </div>
          <strong>{judgment.风险.length}</strong>
        </div>
        <ol className="drawer-numbered-list is-risk">
          {judgment.风险.map((risk, index) => <li key={`${index}-${risk}`}>{risk}</li>)}
        </ol>
      </section>

      {judgment.改判条件?.trim() && (
        <section className="drawer-section drawer-change-trigger">
          <span>决策边界</span>
          <h3>改判条件</h3>
          <p>{judgment.改判条件}</p>
        </section>
      )}

      {instructions.length > 0 && (
        <section className="drawer-section">
          <div className="drawer-section-heading">
            <div>
              <span>执行留痕</span>
              <h3>本轮关联指令</h3>
            </div>
            <strong>{instructions.length}</strong>
          </div>
          <ul className="drawer-instruction-list">
            {instructions.map((instruction) => (
              <li key={instruction.instruction_code}>
                <div className="drawer-instruction-heading">
                  <code>{instruction.instruction_code}</code>
                  <span className={`instruction-status is-${instruction.状态}`}>
                    {statusLabels[instruction.状态]}
                  </span>
                </div>
                <strong><InstructionText instruction={instruction} /></strong>
                <dl>
                  <div><dt>理由</dt><dd>{instruction.理由}</dd></div>
                  <div><dt>风险提示</dt><dd>{instruction.风险提示}</dd></div>
                </dl>
                {instruction.状态 === 'rejected' && (
                  <ol className="drawer-rejection-list" aria-label="拦截原因">
                    {instruction.拦截原因.map((reason) => (
                      <li key={`${reason.code}-${reason.message}`}>
                        <code>{reason.code}</code>
                        <span>{reason.message}</span>
                      </li>
                    ))}
                  </ol>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      <ContextEvidence recordKey={record.key} run={run} judgment={judgment} />
      <RawRunJson strategyId={run.strategy_id} />
    </>
  )
}

function IssueDetail({
  record,
}: {
  record: Extract<JudgmentRecord, { kind: 'issue' }>
}) {
  return (
    <>
      <section className="drawer-section run-issue-detail">
        <span>未产出原因</span>
        <h3>本轮未产出判断</h3>
        <dl>
          <div><dt>问题代码</dt><dd><code>{record.issue.code}</code></dd></div>
          <div><dt>原因</dt><dd>{record.issue.message}</dd></div>
        </dl>
      </section>
      <RawRunJson strategyId={record.run.strategy_id} />
    </>
  )
}

export function JudgmentDetailDrawer({
  record,
  onClose,
}: {
  record: JudgmentRecord
  onClose: () => void
}) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const closeButtonRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    const dialog = dialogRef.current
    const opener = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null
    const rootOverflow = document.documentElement.style.overflow
    const bodyOverflow = document.body.style.overflow

    document.documentElement.style.overflow = 'hidden'
    document.body.style.overflow = 'hidden'

    if (dialog && !dialog.open) {
      dialog.showModal()
      closeButtonRef.current?.focus()
    }

    return () => {
      if (dialog?.open) {
        dialog.close()
      }
      document.documentElement.style.overflow = rootOverflow
      document.body.style.overflow = bodyOverflow
      window.requestAnimationFrame(() => {
        if (opener?.isConnected) {
          opener.focus()
        }
      })
    }
  }, [])

  function closeDrawer() {
    if (dialogRef.current?.open) {
      dialogRef.current.close()
    }
    onClose()
  }

  const title = record.kind === 'judgment'
    ? record.judgment.名称
    : record.issue.object_id === null
      ? '整轮问题'
      : '本轮未产出判断'
  const objectId = record.kind === 'judgment'
    ? record.judgment.object_id
    : record.issue.object_id ?? '整轮'

  return (
    <dialog
      ref={dialogRef}
      id="judgment-detail-drawer"
      className="judgment-drawer"
      aria-labelledby="judgment-drawer-title"
      onCancel={(event) => {
        event.preventDefault()
        closeDrawer()
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) {
          closeDrawer()
        }
      }}
    >
      <div className="judgment-drawer-shell">
        <header className="judgment-drawer-header">
          <div>
            <span>判断详情</span>
            <code>{objectId}</code>
            <h2 id="judgment-drawer-title">{title}</h2>
          </div>
          <button ref={closeButtonRef} type="button" onClick={closeDrawer} aria-label="关闭判断详情">
            <span aria-hidden="true">×</span>
          </button>
        </header>

        <div className="judgment-drawer-summary">
          <div><span>时间</span><strong>{formatDateTime(record.run.生成时间)}</strong></div>
          {record.kind === 'judgment' ? (
            <>
              <div><span>操作</span><strong>{operationLabels[record.judgment.操作]}</strong></div>
              <div><span>置信度</span><strong>{formatPercent(record.judgment.置信度)}</strong></div>
            </>
          ) : (
            <div><span>状态</span><strong>未产出判断</strong></div>
          )}
        </div>

        <div className="judgment-drawer-body">
          {record.kind === 'judgment'
            ? <DecisionDetail record={record} />
            : <IssueDetail record={record} />}
        </div>
      </div>
    </dialog>
  )
}

export function JudgmentRow({
  record,
  active = false,
  onOpen,
}: {
  record: JudgmentRecord
  active?: boolean
  onOpen: () => void
}) {
  return record.kind === 'issue'
    ? <IssueRow record={record} active={active} onOpen={onOpen} />
    : <DecisionRow record={record} active={active} onOpen={onOpen} />
}
