import {
  useEffect,
  useId,
  useRef,
  useState,
  type FormEvent,
} from 'react'
import {
  api,
  apiErrorMessages,
  type ApiError,
  type TradeObject,
} from '../api'
import { PageHeader } from '../components/PageHeader'
import { ResourceMessage } from '../components/ResourceMessage'
import { useApiResource } from '../hooks/useApiResource'
import { formatNumber } from '../lib/format'
import {
  assetTypeOptions,
  duplicateTradeObjectMessage,
  emptyTradeObjectForm,
  marketOptions,
  tradeObjectToFormState,
  tradeObjectTypeOptions,
  validateTradeObjectDraft,
  type TradeObjectDraftErrors,
  type TradeObjectDraftField,
  type TradeObjectFormState,
} from '../lib/objectDraft'

type EditorState =
  | { kind: 'create' }
  | { kind: 'edit'; object: TradeObject }

const draftFields = [
  'market',
  'symbol',
  '名称',
  '类型',
  '资产类型',
] as const satisfies readonly TradeObjectDraftField[]

const fieldLabels: Record<TradeObjectDraftField, string> = {
  market: '市场',
  symbol: '代码',
  名称: '名称',
  类型: '类型',
  资产类型: '资产类型',
}

function errorCount(errors: TradeObjectDraftErrors) {
  return draftFields.reduce(
    (total, field) => total + (errors[field]?.length ?? 0),
    0,
  )
}

function FieldErrors({
  id,
  messages,
}: {
  id: string
  messages: string[] | undefined
}) {
  if (!messages || messages.length === 0) {
    return null
  }

  return (
    <ul className="field-errors" id={id}>
      {messages.map((message) => <li key={message}>{message}</li>)}
    </ul>
  )
}

function ObjectEditor({
  state,
  objects,
  onCancel,
  onSaved,
}: {
  state: EditorState
  objects: readonly TradeObject[]
  onCancel: () => void
  onSaved: (message: string) => void
}) {
  const formId = useId()
  const summaryRef = useRef<HTMLDivElement>(null)
  const marketFieldRef = useRef<HTMLSelectElement>(null)
  const firstEditableFieldRef = useRef<HTMLInputElement>(null)
  const [form, setForm] = useState<TradeObjectFormState>(() =>
    state.kind === 'edit'
      ? tradeObjectToFormState(state.object)
      : { ...emptyTradeObjectForm },
  )
  const [errors, setErrors] = useState<TradeObjectDraftErrors>({})
  const [requestError, setRequestError] = useState<ApiError | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const editingObjectId = state.kind === 'edit' ? state.object.object_id : undefined
  const identityLocked = state.kind === 'edit'
  const totalErrors = errorCount(errors)

  useEffect(() => {
    const firstField = identityLocked
      ? firstEditableFieldRef.current
      : marketFieldRef.current
    firstField?.focus()
    firstField?.scrollIntoView({ block: 'center' })
  }, [identityLocked])

  const fieldId = (field: TradeObjectDraftField) => `${formId}-${field}`
  const errorId = (field: TradeObjectDraftField) => `${fieldId(field)}-errors`

  function updateField(field: TradeObjectDraftField, value: string) {
    setForm((current) => ({ ...current, [field]: value }))
    setErrors((current) => {
      const next = { ...current }
      delete next[field]
      if (field === 'market' && next.symbol) {
        const remainingSymbolErrors = next.symbol.filter(
          (message) => message !== duplicateTradeObjectMessage,
        )
        if (remainingSymbolErrors.length > 0) {
          next.symbol = remainingSymbolErrors
        } else {
          delete next.symbol
        }
      }
      return next
    })
    setRequestError(null)
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const validation = validateTradeObjectDraft(form, objects, editingObjectId)
    setErrors(validation.errors)
    setRequestError(null)

    if (!validation.ok) {
      requestAnimationFrame(() => summaryRef.current?.focus())
      return
    }

    setSubmitting(true)
    try {
      const response =
        state.kind === 'create'
          ? await api.createObject(validation.draft)
          : await api.updateObject(state.object.object_id, validation.draft)

      if (!response.ok) {
        setRequestError(response.error)
        return
      }

      onSaved(
        state.kind === 'create'
          ? `已新增“${validation.draft.名称}”。`
          : `已保存“${validation.draft.名称}”的修改。`,
      )
    } catch {
      setRequestError({
        code: 'REQUEST_FAILED',
        message: '写入请求未完成，请保留当前内容后重试。',
      })
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section className="object-editor" aria-labelledby={`${formId}-title`}>
      <header>
        <div>
          <h2 id={`${formId}-title`}>
            {state.kind === 'create'
              ? '新增一个关注对象'
              : `正在编辑 ${state.object.object_id} · ${state.object.名称}`}
          </h2>
        </div>
        <p>
          {identityLocked
            ? '只修改名称、类型与资产类型；对象身份保持不变。'
            : '只提交五个维护字段；对象 ID 由后端生成，交易单位由后端提供。'}
        </p>
      </header>

      <form noValidate onSubmit={handleSubmit}>
        {totalErrors > 0 && (
          <div
            className="form-error-summary"
            ref={summaryRef}
            role="alert"
            tabIndex={-1}
          >
            <strong>请检查以下 {totalErrors} 项</strong>
            <ul>
              {draftFields.flatMap((field) =>
                (errors[field] ?? []).map((message) => (
                  <li key={`${field}-${message}`}>
                    <a href={`#${fieldId(field)}`}>{fieldLabels[field]}：{message}</a>
                  </li>
                )),
              )}
            </ul>
          </div>
        )}

        {requestError && (
          <div className="form-request-error" role="alert">
            <strong>没有保存</strong>
            <ul className="api-error-list">
              {apiErrorMessages(requestError).map((message, index) => (
                <li key={`${index}-${message}`}>{message}</li>
              ))}
            </ul>
          </div>
        )}

        {identityLocked && (
          <p className="identity-lock-note" id={`${formId}-identity-lock`}>
            市场和代码用于确定标的身份，编辑时不可修改。要改代码请删除后重新添加。
          </p>
        )}

        <div className="object-form-grid">
          <label className="form-field" htmlFor={fieldId('market')}>
            <span>市场</span>
            <select
              ref={marketFieldRef}
              id={fieldId('market')}
              value={form.market}
              required
              disabled={identityLocked}
              aria-invalid={Boolean(errors.market)}
              aria-describedby={
                identityLocked
                  ? `${formId}-identity-lock`
                  : errors.market
                    ? errorId('market')
                    : undefined
              }
              onChange={(event) => updateField('market', event.target.value)}
            >
              <option value="">请选择</option>
              {marketOptions.map((market) => <option key={market} value={market}>{market}</option>)}
            </select>
            <FieldErrors id={errorId('market')} messages={errors.market} />
          </label>

          <label className="form-field" htmlFor={fieldId('symbol')}>
            <span>代码</span>
            <input
              id={fieldId('symbol')}
              type="text"
              inputMode="numeric"
              autoComplete="off"
              value={form.symbol}
              required
              disabled={identityLocked}
              aria-invalid={Boolean(errors.symbol)}
              aria-describedby={
                identityLocked
                  ? `${formId}-identity-lock`
                  : errors.symbol
                    ? errorId('symbol')
                    : undefined
              }
              onChange={(event) => updateField('symbol', event.target.value)}
            />
            <FieldErrors id={errorId('symbol')} messages={errors.symbol} />
          </label>

          <label className="form-field" htmlFor={fieldId('名称')}>
            <span>名称</span>
            <input
              ref={firstEditableFieldRef}
              id={fieldId('名称')}
              type="text"
              autoComplete="off"
              value={form.名称}
              required
              aria-invalid={Boolean(errors.名称)}
              aria-describedby={errors.名称 ? errorId('名称') : undefined}
              onChange={(event) => updateField('名称', event.target.value)}
            />
            <FieldErrors id={errorId('名称')} messages={errors.名称} />
          </label>

          <label className="form-field" htmlFor={fieldId('类型')}>
            <span>类型</span>
            <select
              id={fieldId('类型')}
              value={form.类型}
              required
              aria-invalid={Boolean(errors.类型)}
              aria-describedby={`${fieldId('类型')}-hint${errors.类型 ? ` ${errorId('类型')}` : ''}`}
              onChange={(event) => updateField('类型', event.target.value)}
            >
              <option value="">请选择</option>
              {tradeObjectTypeOptions.map((type) => <option key={type} value={type}>{type}</option>)}
            </select>
            <small id={`${fieldId('类型')}-hint`}>交易标的可产生指令；行情对象只作为判断背景。</small>
            <FieldErrors id={errorId('类型')} messages={errors.类型} />
          </label>

          <label className="form-field" htmlFor={fieldId('资产类型')}>
            <span>资产类型</span>
            <select
              id={fieldId('资产类型')}
              value={form.资产类型}
              required
              aria-invalid={Boolean(errors.资产类型)}
              aria-describedby={errors.资产类型 ? errorId('资产类型') : undefined}
              onChange={(event) => updateField('资产类型', event.target.value)}
            >
              <option value="">请选择</option>
              {assetTypeOptions.map((type) => <option key={type} value={type}>{type}</option>)}
            </select>
            <FieldErrors id={errorId('资产类型')} messages={errors.资产类型} />
          </label>
        </div>

        <footer className="object-form-actions">
          <button type="button" className="quiet-action" onClick={onCancel} disabled={submitting}>
            取消
          </button>
          <button type="submit" className="primary-action" disabled={submitting}>
            {submitting ? '保存中…' : state.kind === 'create' ? '新增标的' : '保存修改'}
          </button>
        </footer>
      </form>
    </section>
  )
}

function HoldingEmptyCell({ label }: { label: string }) {
  return (
    <td className="holding-not-applicable" data-label={label} role="cell">
      <span className="sr-only">不适用</span>
    </td>
  )
}

function HoldingUncollectedCell({ label }: { label: string }) {
  return (
    <td className="holding-uncollected" data-label={label} role="cell">
      未采集
    </td>
  )
}

function ObjectRow({
  object,
  onEdit,
  onDelete,
}: {
  object: TradeObject
  onEdit: (opener: HTMLButtonElement) => void
  onDelete: () => void
}) {
  const isMarketObject = object.类型 === '行情对象'

  return (
    <tr className={isMarketObject ? 'is-market-object' : ''} role="row">
      <th scope="row" data-label="标的" role="rowheader">
        <code>{object.object_id}</code>
        <strong>{object.名称}</strong>
      </th>
      <td data-label="类型" role="cell">
        <span className={`object-purpose ${isMarketObject ? 'is-reference' : ''}`}>
          <strong>{object.类型}</strong>
          <small>{isMarketObject ? '仅作判断背景' : '可产生指令'}</small>
        </span>
      </td>
      <td data-label="资产 / 单位" role="cell">
        <span className="asset-unit">
          <strong>{object.资产类型}</strong>
          <small>{formatNumber(object.交易单位)} 股/手</small>
        </span>
      </td>
      {isMarketObject ? (
        <HoldingEmptyCell label="持仓 / 可用" />
      ) : object.持仓 === null ? (
        <HoldingUncollectedCell label="持仓 / 可用" />
      ) : (
        <td data-label="持仓 / 可用" role="cell">
          {formatNumber(object.持仓.持仓数量)} / {formatNumber(object.持仓.可用数量)}
        </td>
      )}
      <td className="object-row-actions" data-label="操作" role="cell">
        <button
          type="button"
          className="text-action"
          aria-label={`编辑 ${object.object_id} ${object.名称}`}
          onClick={(event) => onEdit(event.currentTarget)}
        >
          编辑
        </button>
        <button
          type="button"
          className="text-action is-danger"
          aria-label={`删除 ${object.object_id} ${object.名称}`}
          onClick={onDelete}
        >
          删除
        </button>
      </td>
    </tr>
  )
}

function DeleteObjectDialog({
  object,
  onCancel,
  onDeleted,
}: {
  object: TradeObject
  onCancel: () => void
  onDeleted: (message: string) => void
}) {
  const titleId = useId()
  const descriptionId = useId()
  const targetId = useId()
  const dialogRef = useRef<HTMLDialogElement>(null)
  const cancelRef = useRef<HTMLButtonElement>(null)
  const [submitting, setSubmitting] = useState(false)
  const [requestError, setRequestError] = useState<ApiError | null>(null)
  const onCancelRef = useRef(onCancel)
  const submittingRef = useRef(submitting)
  onCancelRef.current = onCancel
  submittingRef.current = submitting

  useEffect(() => {
    const previousFocus = document.activeElement
    const dialog = dialogRef.current
    dialog?.showModal()
    cancelRef.current?.focus()

    function handleCancel(event: Event) {
      event.preventDefault()
      if (!submittingRef.current) {
        onCancelRef.current()
      }
    }

    dialog?.addEventListener('cancel', handleCancel)
    return () => {
      dialog?.removeEventListener('cancel', handleCancel)
      if (dialog?.open) {
        dialog.close()
      }
      if (previousFocus instanceof HTMLElement && previousFocus.isConnected) {
        previousFocus.focus()
      } else {
        document.querySelector<HTMLElement>('#objects-ledger-title')?.focus()
      }
    }
  }, [])

  async function handleDelete() {
    dialogRef.current?.focus()
    setSubmitting(true)
    setRequestError(null)
    try {
      const response = await api.deleteObject(object.object_id)
      if (!response.ok) {
        if (response.error.code === 'NOT_FOUND') {
          onDeleted(`标的 ${object.object_id} 已不在清单中，已刷新当前列表。`)
          return
        }
        setRequestError(response.error)
        return
      }
      onDeleted(`已删除 ${object.object_id} ${object.名称}。`)
    } catch {
      setRequestError({
        code: 'REQUEST_FAILED',
        message: '删除请求未完成，请重试。',
      })
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <dialog
      ref={dialogRef}
      className="delete-object-dialog"
      aria-labelledby={titleId}
      aria-describedby={`${targetId} ${descriptionId}`}
      tabIndex={-1}
    >
        <header>
          <h2 id={titleId}>删除“{object.名称}”？</h2>
        </header>

        <dl className="delete-object-target" id={targetId}>
          <div><dt>代码</dt><dd><code>{object.object_id}</code></dd></div>
          <div><dt>名称</dt><dd>{object.名称}</dd></div>
          <div><dt>类型</dt><dd>{object.类型}</dd></div>
          {object.类型 === '行情对象' ? (
            <div><dt>当前持仓</dt><dd>不适用（行情对象不可交易）</dd></div>
          ) : object.持仓 === null ? (
            <>
              <div><dt>当前持仓</dt><dd className="holding-uncollected">未采集</dd></div>
              <div><dt>当前可用</dt><dd className="holding-uncollected">未采集</dd></div>
            </>
          ) : (
            <>
              <div>
                <dt>当前持仓</dt>
                <dd>
                  {formatNumber(object.持仓.持仓数量)} 股
                  {!object.持仓.是否持仓 && '（无持仓）'}
                </dd>
              </div>
              <div>
                <dt>当前可用</dt>
                <dd>{formatNumber(object.持仓.可用数量)} 股</dd>
              </div>
            </>
          )}
        </dl>

        <p id={descriptionId}>
          删除后，系统从此不再关注这个标的。请核对代码、名称和当前持仓后再继续。
        </p>

        {requestError && (
          <div className="dialog-error" role="alert">
            <strong>没有删除</strong>
            <ul className="api-error-list">
              {apiErrorMessages(requestError).map((message, index) => (
                <li key={`${index}-${message}`}>{message}</li>
              ))}
            </ul>
          </div>
        )}

        <footer>
          <button
            ref={cancelRef}
            type="button"
            className="quiet-action"
            onClick={onCancel}
            disabled={submitting}
          >
            取消
          </button>
          <button
            type="button"
            className="danger-action"
            onClick={() => void handleDelete()}
            disabled={submitting}
          >
            {submitting ? '删除中…' : `删除 ${object.object_id}`}
          </button>
        </footer>
    </dialog>
  )
}

export function ObjectsPage() {
  const objects = useApiResource(api.getObjects)
  const [editor, setEditor] = useState<EditorState | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<TradeObject | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const editorOpenerRef = useRef<HTMLElement | null>(null)
  const ledgerHeadingRef = useRef<HTMLHeadingElement>(null)
  const count = objects.status === 'success' ? objects.data.length : null

  function startEditor(next: EditorState, opener: HTMLElement) {
    editorOpenerRef.current = opener
    setNotice(null)
    setEditor(next)
  }

  function cancelEditor() {
    setEditor(null)
    requestAnimationFrame(() => {
      if (editorOpenerRef.current?.isConnected) {
        editorOpenerRef.current.focus()
      } else {
        ledgerHeadingRef.current?.focus()
      }
    })
  }

  function completeMutation(message: string) {
    setNotice(message)
    setEditor(null)
    setDeleteTarget(null)
    objects.reload()
    requestAnimationFrame(() => ledgerHeadingRef.current?.focus())
  }

  return (
    <section className="objects-page page-enter">
      <PageHeader
        title="标的管理"
        description="维护交易标的和行情对象，查看当前持仓状态。"
        aside={count === null ? '标的清单' : `共 ${count} 个`}
      />

      {notice && <div className="mutation-notice" role="status">{notice}</div>}

      {editor && objects.status === 'success' && (
        <ObjectEditor
          key={editor.kind === 'create' ? 'create' : editor.object.object_id}
          state={editor}
          objects={objects.data}
          onCancel={cancelEditor}
          onSaved={completeMutation}
        />
      )}

      <section className="ledger-section" aria-labelledby="objects-ledger-title">
        <header className="section-heading objects-section-heading">
          <div>
            <h2 id="objects-ledger-title" ref={ledgerHeadingRef} tabIndex={-1}>标的清单</h2>
          </div>
          {objects.status === 'success' && (
            <button
              type="button"
              className="primary-action"
              onClick={(event) => startEditor({ kind: 'create' }, event.currentTarget)}
            >
              新增标的
            </button>
          )}
        </header>

        {objects.status === 'loading' && (
          <ResourceMessage kind="loading" title="正在读取标的清单" message="读取交易标的与行情对象。" />
        )}
        {objects.status === 'error' && (
          <ResourceMessage
            kind="error"
            title="标的清单读取失败"
            message={objects.error}
            apiError={objects.apiError}
            onRetry={objects.reload}
          />
        )}
        {objects.status === 'success' && objects.data.length === 0 && (
          <div className="objects-empty" role="status">
            <span aria-hidden="true">○</span>
            <div>
              <strong>还没有标的</strong>
              <p>新增第一个交易标的或行情对象后，系统才知道要持续关注什么。</p>
            </div>
            <button
              type="button"
              className="primary-action"
              onClick={(event) => startEditor({ kind: 'create' }, event.currentTarget)}
            >
              新增第一个标的
            </button>
          </div>
        )}
        {objects.status === 'success' && objects.data.length > 0 && (
          <div className="objects-table-wrap">
            <table className="objects-table" role="table">
              <colgroup>
                <col className="object-column-identity" />
                <col className="object-column-type" />
                <col className="object-column-asset" />
                <col className="object-column-position" />
                <col className="object-column-actions" />
              </colgroup>
              <thead role="rowgroup">
                <tr role="row">
                  <th scope="col" role="columnheader">标的</th>
                  <th scope="col" role="columnheader">类型</th>
                  <th scope="col" role="columnheader">资产 / 单位</th>
                  <th scope="col" role="columnheader">持仓 / 可用</th>
                  <th scope="col" role="columnheader">操作</th>
                </tr>
              </thead>
              <tbody role="rowgroup">
                {objects.data.map((object) => (
                  <ObjectRow
                    key={object.object_id}
                    object={object}
                    onEdit={(opener) => startEditor({ kind: 'edit', object }, opener)}
                    onDelete={() => {
                      setNotice(null)
                      setEditor(null)
                      setDeleteTarget(object)
                    }}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {deleteTarget && (
        <DeleteObjectDialog
          object={deleteTarget}
          onCancel={() => setDeleteTarget(null)}
          onDeleted={completeMutation}
        />
      )}
    </section>
  )
}
