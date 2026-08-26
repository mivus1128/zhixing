import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { useOutletContext } from 'react-router-dom'
import {
  api,
  apiErrorMessages,
  type BrokerSettingsInput,
  type CaptchaRecognitionMethod,
  type CaptchaRecognizerSettings,
  type CaptchaSettingsInput,
  type ModelSettingsDraft,
} from '../api'
import { PageHeader } from '../components/PageHeader'
import { ResourceMessage } from '../components/ResourceMessage'
import { UsageOverview } from '../components/UsageOverview'
import { useApiResource } from '../hooks/useApiResource'
import type { AppOutletContext } from '../layout/AppLayout'
import '../styles/runtime.css'

interface MutationFeedbackProps {
  errors: string[] | null
  notice: string | null
}

function MutationFeedback({ errors, notice }: MutationFeedbackProps) {
  return (
    <>
      {errors && (
        <div className="runtime-inline-error" role="alert">
          {errors.length === 1 ? (
            errors[0]
          ) : (
            <>
              <strong>请一次检查以下 {errors.length} 项</strong>
              <ul>
                {errors.map((message) => <li key={message}>{message}</li>)}
              </ul>
            </>
          )}
        </div>
      )}
      {notice && <div className="runtime-inline-notice" role="status">{notice}</div>}
    </>
  )
}

function UnattendedSection({ systemStatus }: AppOutletContext) {
  const fieldId = useId()
  const [target, setTarget] = useState('')
  const [reason, setReason] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [requestErrors, setRequestErrors] = useState<string[] | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const targetEnabled = target === 'enabled'
  const repeatsCurrentState =
    target !== '' &&
    systemStatus.status === 'success' &&
    targetEnabled === systemStatus.data.无人值守
  const canSubmit =
    systemStatus.status === 'success' &&
    target !== '' &&
    reason.trim().length > 0 &&
    !repeatsCurrentState &&
    !submitting

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setRequestErrors(null)
    setNotice(null)

    if (!reason.trim()) {
      setRequestErrors(['必须填写变更原因，后端会把它写入审计记录。'])
      return
    }
    if (target === '') {
      setRequestErrors(['请选择要切换到的目标状态。'])
      return
    }
    if (systemStatus.status !== 'success') {
      setRequestErrors(['当前状态尚不可用，不能在未知状态下变更无人值守模式。'])
      return
    }
    if (repeatsCurrentState) {
      setRequestErrors(['所选目标与顶栏当前状态一致，无需重复提交。'])
      return
    }

    setSubmitting(true)
    try {
      const response = await api.putUnattended({
        无人值守: targetEnabled,
        原因: reason.trim(),
      })
      if (!response.ok) {
        setRequestErrors(apiErrorMessages(response.error))
        return
      }

      setTarget('')
      setReason('')
      setNotice('无人值守模式已变更，原因已随本次操作留痕。')
      systemStatus.reload()
    } catch {
      setRequestErrors(['变更请求未完成，请检查连接后重试。'])
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section className="ledger-section runtime-section" aria-labelledby="unattended-title">
      <header className="section-heading">
        <div>
          <h2 id="unattended-title">无人值守</h2>
          <p>当前状态只在顶栏常驻显示；这里仅提交目标状态与必填原因。</p>
        </div>
      </header>

      <form className="runtime-form unattended-form" onSubmit={handleSubmit}>
        <label className="runtime-field" htmlFor={`${fieldId}-target`}>
          <span>目标状态</span>
          <select
            id={`${fieldId}-target`}
            value={target}
            onChange={(event) => {
              setTarget(event.target.value)
              setRequestErrors(null)
              setNotice(null)
            }}
          >
            <option value="">请选择变更动作</option>
            <option value="enabled">开启无人值守</option>
            <option value="disabled">关闭无人值守</option>
          </select>
          <small>{repeatsCurrentState ? '所选状态与顶栏一致。' : '提交后顶栏会重新读取状态。'}</small>
        </label>

        <label className="runtime-field unattended-reason" htmlFor={`${fieldId}-reason`}>
          <span>变更原因 <b>必填</b></span>
          <textarea
            id={`${fieldId}-reason`}
            rows={3}
            required
            value={reason}
            placeholder="说明为什么要改变运行方式；此内容会被后端留痕"
            onChange={(event) => {
              setReason(event.target.value)
              setRequestErrors(null)
              setNotice(null)
            }}
          />
        </label>

        <div className="runtime-form-actions">
          {systemStatus.status !== 'success' && (
            <span className="runtime-action-hint is-error">状态未知时禁止变更。</span>
          )}
          <button type="submit" className="primary-action" disabled={!canSubmit}>
            {submitting ? '正在提交…' : '提交模式变更'}
          </button>
        </div>
      </form>
      <MutationFeedback errors={requestErrors} notice={notice} />
    </section>
  )
}

function validateSchedule(times: readonly string[]): string[] {
  const problems: string[] = []
  if (times.length !== 6) {
    problems.push(`必须恰好填写六个调度时点，当前收到 ${times.length} 个。`)
  }

  const validTimes: { index: number; value: string }[] = []
  times.forEach((time, index) => {
    if (!time) {
      problems.push(`第 ${index + 1} 个调度时点为空。`)
    } else if (!/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(time)) {
      problems.push(`第 ${index + 1} 个调度时点“${time}”不是合法的 HH:MM。`)
    } else {
      validTimes.push({ index, value: time })
    }
  })

  const firstIndexByTime = new Map<string, number>()
  validTimes.forEach(({ index, value }) => {
    const firstIndex = firstIndexByTime.get(value)
    if (firstIndex === undefined) {
      firstIndexByTime.set(value, index)
    } else {
      problems.push(`第 ${index + 1} 个时点 ${value} 与第 ${firstIndex + 1} 个重复。`)
    }
  })

  for (let index = 1; index < validTimes.length; index += 1) {
    const previous = validTimes[index - 1]
    const current = validTimes[index]
    if (previous && current && current.value < previous.value) {
      problems.push(
        `第 ${current.index + 1} 个时点 ${current.value} 早于第 ${previous.index + 1} 个 ${previous.value}，时点必须按时间升序。`,
      )
    }
  }

  return problems
}

function ScheduleSection() {
  const fieldId = useId()
  const schedule = useApiResource(api.getSchedule)
  const [times, setTimes] = useState<string[]>(Array.from({ length: 6 }, () => ''))
  const [reason, setReason] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [requestErrors, setRequestErrors] = useState<string[] | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  useEffect(() => {
    if (schedule.status === 'success') {
      setTimes([...schedule.data.时点])
    }
  }, [schedule.data, schedule.status])

  function updateTime(index: number, value: string) {
    setTimes((current) => current.map((time, candidate) => candidate === index ? value : time))
    setRequestErrors(null)
    setNotice(null)
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setRequestErrors(null)
    setNotice(null)
    const validationErrors = validateSchedule(times)
    if (!reason.trim()) {
      validationErrors.push('必须填写变更原因，后端会把它写入审计记录。')
    }
    if (validationErrors.length > 0) {
      setRequestErrors(validationErrors)
      return
    }

    setSubmitting(true)
    try {
      const response = await api.putSchedule({
        时点: times,
        原因: reason.trim(),
      })
      if (!response.ok) {
        setRequestErrors(apiErrorMessages(response.error))
        return
      }
      setReason('')
      setNotice('六个调度时点已写入。')
      schedule.reload()
    } catch {
      setRequestErrors(['调度配置未写入，请检查连接后重试。'])
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section className="ledger-section runtime-section" aria-labelledby="schedule-title">
      <header className="section-heading">
        <div>
          <h2 id="schedule-title">调度时间</h2>
          <p>每个工作日六个时点，按 24 小时制填写。</p>
        </div>
      </header>

      {schedule.status === 'loading' && (
        <ResourceMessage kind="loading" title="正在读取调度计划" message="加载六个运行时点。" />
      )}
      {schedule.status === 'error' && (
        <ResourceMessage
          kind="error"
          title="调度计划读取失败"
          message={schedule.error}
          apiError={schedule.apiError}
          onRetry={schedule.reload}
        />
      )}
      {schedule.status === 'success' && (
        <form className="runtime-form schedule-form" onSubmit={handleSubmit}>
          <fieldset>
            <legend className="sr-only">六个调度时点</legend>
            <ol className="schedule-list">
              {times.map((time, index) => (
                <li className="schedule-row" key={index}>
                  <label htmlFor={`${fieldId}-${index}`}>时点 {index + 1}</label>
                  <input
                    id={`${fieldId}-${index}`}
                    type="time"
                    step={60}
                    required
                    value={time}
                    onChange={(event) => updateTime(index, event.target.value)}
                  />
                </li>
              ))}
            </ol>
          </fieldset>
          <label className="runtime-field schedule-reason" htmlFor={`${fieldId}-reason`}>
            <span>变更原因 <b>必填</b></span>
            <textarea
              id={`${fieldId}-reason`}
              rows={2}
              required
              value={reason}
              placeholder="说明为什么调整六个时点；此内容会被后端留痕"
              onChange={(event) => {
                setReason(event.target.value)
                setRequestErrors(null)
                setNotice(null)
              }}
            />
          </label>
          <div className="runtime-form-actions">
            <button type="submit" className="primary-action" disabled={submitting}>
              {submitting ? '正在保存…' : '保存调度计划'}
            </button>
          </div>
        </form>
      )}
      <MutationFeedback errors={requestErrors} notice={notice} />
    </section>
  )
}

interface CaptchaRecognizerIdentity {
  endpoint: string
  model: string
  method: CaptchaRecognitionMethod
}

interface CaptchaBackupDraft extends CaptchaRecognizerIdentity {
  id: string
  currentSecret: string
  newSecret: string
  original: CaptchaRecognizerIdentity | null
}

const captchaMethods: ReadonlyArray<{
  value: CaptchaRecognitionMethod
  label: string
  note: string
}> = [
  { value: 'vision', label: '视觉模型', note: '兼容图像输入的模型接口' },
  { value: 'ttshitu', label: '图鉴', note: '图鉴验证码识别接口' },
  { value: 'chaojiying', label: '超级鹰', note: '超级鹰验证码识别接口' },
]

function captchaIdentity(settings: CaptchaRecognizerSettings): CaptchaRecognizerIdentity {
  return {
    endpoint: settings.接口地址,
    model: settings.模型,
    method: settings.识别方式,
  }
}

function sameCaptchaIdentity(
  left: CaptchaRecognizerIdentity | null,
  right: CaptchaRecognizerIdentity,
): boolean {
  return left !== null
    && left.endpoint === right.endpoint.trim()
    && left.model === right.model.trim()
    && left.method === right.method
}

function CaptchaSection() {
  const fieldId = useId()
  const backupId = useRef(0)
  const captcha = useApiResource(api.getCaptcha)
  const [endpoint, setEndpoint] = useState('')
  const [model, setModel] = useState('')
  const [method, setMethod] = useState<CaptchaRecognitionMethod>('vision')
  const [currentSecret, setCurrentSecret] = useState('')
  const [originalPrimary, setOriginalPrimary] = useState<CaptchaRecognizerIdentity | null>(null)
  const [newSecret, setNewSecret] = useState('')
  const [backups, setBackups] = useState<CaptchaBackupDraft[]>([])
  const [submitting, setSubmitting] = useState(false)
  const [requestErrors, setRequestErrors] = useState<string[] | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  useEffect(() => {
    if (captcha.status === 'success') {
      setEndpoint(captcha.data.接口地址)
      setModel(captcha.data.模型)
      setMethod(captcha.data.识别方式)
      setCurrentSecret(captcha.data.密钥)
      setOriginalPrimary(captchaIdentity(captcha.data))
      setNewSecret('')
      setBackups(captcha.data.备用识别.map((recognizer, index) => ({
        id: `${fieldId}-saved-backup-${index}`,
        ...captchaIdentity(recognizer),
        currentSecret: recognizer.密钥,
        newSecret: '',
        original: captchaIdentity(recognizer),
      })))
    }
  }, [captcha.data, captcha.status, fieldId])

  function clearFeedback() {
    setRequestErrors(null)
    setNotice(null)
  }

  function updateBackup(id: string, patch: Partial<CaptchaBackupDraft>) {
    setBackups((current) => current.map((draft) => (
      draft.id === id ? { ...draft, ...patch } : draft
    )))
    clearFeedback()
  }

  function addBackup() {
    backupId.current += 1
    setBackups((current) => [
      ...current,
      {
        id: `${fieldId}-new-backup-${backupId.current}`,
        endpoint: '',
        model: '',
        method: 'vision',
        currentSecret: '',
        newSecret: '',
        original: null,
      },
    ])
    clearFeedback()
  }

  function removeUnsavedBackup(id: string) {
    setBackups((current) => current.filter((draft) => draft.id !== id || draft.original !== null))
    clearFeedback()
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setRequestErrors(null)
    setNotice(null)
    const errors: string[] = []
    const primaryIdentity = { endpoint, model, method }
    if (!endpoint.trim()) {
      errors.push('主识别服务的接口地址不能为空。')
    }
    if (method === 'vision' && !model.trim()) {
      errors.push('主识别服务使用视觉模型时，必须填写模型。')
    }
    if (!newSecret.trim() && (!currentSecret.trim() || !sameCaptchaIdentity(originalPrimary, primaryIdentity))) {
      errors.push('主识别服务是首次配置或已更换服务，请填写新密钥。')
    }

    backups.forEach((draft, index) => {
      const label = `备用识别 ${index + 1}`
      if (!draft.endpoint.trim()) {
        errors.push(`${label} 的接口地址不能为空。`)
      }
      if (draft.method === 'vision' && !draft.model.trim()) {
        errors.push(`${label} 使用视觉模型时，必须填写模型。`)
      }
      if (!draft.newSecret.trim() && (!draft.currentSecret.trim() || !sameCaptchaIdentity(draft.original, draft))) {
        errors.push(`${label} 是新增或已更换的服务，请填写新密钥。`)
      }
    })

    if (errors.length > 0) {
      setRequestErrors(errors)
      return
    }

    const input: CaptchaSettingsInput = {
      接口地址: endpoint.trim(),
      模型: model.trim(),
      识别方式: method,
      密钥: newSecret.trim(),
      备用识别: backups.map((draft) => ({
        接口地址: draft.endpoint.trim(),
        模型: draft.model.trim(),
        识别方式: draft.method,
        密钥: draft.newSecret.trim(),
      })),
    }

    setSubmitting(true)
    try {
      const response = await api.putCaptcha(input)
      if (!response.ok) {
        setRequestErrors(apiErrorMessages(response.error))
        return
      }
      setNewSecret('')
      setBackups((current) => current.map((draft) => ({ ...draft, newSecret: '' })))
      const changedSecretCount = Number(input.密钥.length > 0)
        + input.备用识别.filter((recognizer) => recognizer.密钥.length > 0).length
      setNotice(
        changedSecretCount > 0
          ? `验证码服务已写入，更新了 ${changedSecretCount} 个密钥；明文不会回显。`
          : '验证码服务已写入；空白密钥没有覆盖现值。',
      )
      captcha.reload()
    } catch {
      setRequestErrors(['验证码接口配置未写入，请检查连接后重试。'])
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section className="ledger-section runtime-section" aria-labelledby="captcha-title">
      <header className="section-heading">
        <div>
          <h2 id="captcha-title">验证码服务</h2>
          <p>配置主识别服务与按顺序降级的备用服务；后端只返回脱敏密钥。</p>
        </div>
      </header>

      {captcha.status === 'loading' && (
        <ResourceMessage kind="loading" title="正在读取接口配置" message="密钥只会返回脱敏值。" />
      )}
      {captcha.status === 'error' && (
        <ResourceMessage
          kind="error"
          title="验证码接口读取失败"
          message={captcha.error}
          apiError={captcha.apiError}
          onRetry={captcha.reload}
        />
      )}
      {captcha.status === 'success' && (
        <form className="runtime-form captcha-form" onSubmit={handleSubmit}>
          <div className="captcha-group-heading">
            <div>
              <span>PRIMARY</span>
              <strong>主识别服务</strong>
            </div>
            <small>每次识别先走这一条</small>
          </div>
          <div className="captcha-settings-list">
            <label className="captcha-setting-row" htmlFor={`${fieldId}-method`}>
              <span>识别方式</span>
              <select
                id={`${fieldId}-method`}
                value={method}
                onChange={(event) => {
                  setMethod(event.target.value as CaptchaRecognitionMethod)
                  clearFeedback()
                }}
              >
                {captchaMethods.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
              <small>{captchaMethods.find((option) => option.value === method)?.note}</small>
            </label>
            <label className="captcha-setting-row" htmlFor={`${fieldId}-endpoint`}>
              <span>接口地址</span>
              <input
                id={`${fieldId}-endpoint`}
                type="url"
                required
                value={endpoint}
                onChange={(event) => {
                  setEndpoint(event.target.value)
                  clearFeedback()
                }}
              />
              <small>HTTPS 接口地址</small>
            </label>
            <label className="captcha-setting-row" htmlFor={`${fieldId}-model`}>
              <span>识别模型</span>
              <input
                id={`${fieldId}-model`}
                type="text"
                required={method === 'vision'}
                value={model}
                placeholder={method === 'vision' ? '填写模型名称' : '该服务不需要时可留空'}
                onChange={(event) => {
                  setModel(event.target.value)
                  clearFeedback()
                }}
              />
              <small>{method === 'vision' ? '由接口提供方定义' : '当前识别方式通常无需模型名'}</small>
            </label>
            <div className="captcha-setting-row captcha-secret-mask">
              <span>当前密钥</span>
              <code>{currentSecret || '尚未配置'}</code>
              <small>只读脱敏值</small>
            </div>
            <label className="captcha-setting-row" htmlFor={`${fieldId}-secret`}>
              <span>新密钥</span>
              <input
                id={`${fieldId}-secret`}
                type="password"
                autoComplete="new-password"
                value={newSecret}
                placeholder="留空表示不修改"
                onChange={(event) => {
                  setNewSecret(event.target.value)
                  clearFeedback()
                }}
              />
              <small>填写后覆盖，提交后仍不回显明文</small>
            </label>
          </div>
          <div className="captcha-backup-section">
            <div className="captcha-backup-heading">
              <div>
                <span>FALLBACK CHAIN</span>
                <strong>备用识别</strong>
              </div>
              <button type="button" className="quiet-action captcha-add-button" onClick={addBackup}>
                + 添加备用服务
              </button>
            </div>
            <p className="captcha-backup-note">
              备用项按显示顺序尝试。已保存项目必须保持顺序；更换其方式、地址或模型时，请同时填写该项的新密钥。
            </p>
            {backups.length === 0 && (
              <div className="captcha-backup-empty">当前没有备用服务，主服务失败时不会自动切换。</div>
            )}
            {backups.map((draft, index) => (
              <section className="captcha-backup-card" key={draft.id} aria-labelledby={`${draft.id}-title`}>
                <header>
                  <div>
                    <span>{String(index + 1).padStart(2, '0')}</span>
                    <strong id={`${draft.id}-title`}>备用识别 {index + 1}</strong>
                    <small>{draft.original === null ? '未保存' : '已保存'}</small>
                  </div>
                  {draft.original === null && (
                    <button
                      type="button"
                      className="quiet-action"
                      onClick={() => removeUnsavedBackup(draft.id)}
                    >
                      移除未保存项
                    </button>
                  )}
                </header>
                <div className="captcha-settings-list">
                  <label className="captcha-setting-row" htmlFor={`${draft.id}-method`}>
                    <span>识别方式</span>
                    <select
                      id={`${draft.id}-method`}
                      value={draft.method}
                      onChange={(event) => updateBackup(draft.id, {
                        method: event.target.value as CaptchaRecognitionMethod,
                      })}
                    >
                      {captchaMethods.map((option) => (
                        <option key={option.value} value={option.value}>{option.label}</option>
                      ))}
                    </select>
                    <small>{captchaMethods.find((option) => option.value === draft.method)?.note}</small>
                  </label>
                  <label className="captcha-setting-row" htmlFor={`${draft.id}-endpoint`}>
                    <span>接口地址</span>
                    <input
                      id={`${draft.id}-endpoint`}
                      type="url"
                      required
                      value={draft.endpoint}
                      onChange={(event) => updateBackup(draft.id, { endpoint: event.target.value })}
                    />
                    <small>HTTPS 接口地址</small>
                  </label>
                  <label className="captcha-setting-row" htmlFor={`${draft.id}-model`}>
                    <span>识别模型</span>
                    <input
                      id={`${draft.id}-model`}
                      type="text"
                      required={draft.method === 'vision'}
                      value={draft.model}
                      placeholder={draft.method === 'vision' ? '填写模型名称' : '该服务不需要时可留空'}
                      onChange={(event) => updateBackup(draft.id, { model: event.target.value })}
                    />
                    <small>{draft.method === 'vision' ? '由接口提供方定义' : '当前识别方式通常无需模型名'}</small>
                  </label>
                  <div className="captcha-setting-row captcha-secret-mask">
                    <span>当前密钥</span>
                    <code>{draft.currentSecret || '尚未配置'}</code>
                    <small>只读脱敏值</small>
                  </div>
                  <label className="captcha-setting-row" htmlFor={`${draft.id}-secret`}>
                    <span>新密钥</span>
                    <input
                      id={`${draft.id}-secret`}
                      type="password"
                      autoComplete="new-password"
                      value={draft.newSecret}
                      placeholder={draft.original === null ? '新增项必须填写' : '留空表示不修改'}
                      onChange={(event) => updateBackup(draft.id, { newSecret: event.target.value })}
                    />
                    <small>只提交本次输入，不会把脱敏值回传</small>
                  </label>
                </div>
              </section>
            ))}
          </div>
          <div className="runtime-form-actions">
            <button type="submit" className="primary-action" disabled={submitting}>
              {submitting ? '正在保存…' : '保存接口配置'}
            </button>
          </div>
        </form>
      )}
      <MutationFeedback errors={requestErrors} notice={notice} />
    </section>
  )
}

interface ModelSettingsProblem {
  code: 'ENDPOINT_SCHEME' | 'MODEL_REQUIRED' | 'PROVIDER_REQUIRED' | 'UNKNOWN_PROTOCOL'
  message: string
}

function validateModelSettings(settings: ModelSettingsDraft): ModelSettingsProblem[] {
  const problems: ModelSettingsProblem[] = []
  if (!settings.接口地址.startsWith('http://') && !settings.接口地址.startsWith('https://')) {
    problems.push({
      code: 'ENDPOINT_SCHEME',
      message: '接口地址必须以 http:// 或 https:// 开头。',
    })
  }
  if (!settings.模型.trim()) {
    problems.push({ code: 'MODEL_REQUIRED', message: '模型不能为空。' })
  }
  if (!settings.提供方.trim()) {
    problems.push({ code: 'PROVIDER_REQUIRED', message: '提供方不能为空。' })
  }
  if (!['openai_chat', 'anthropic_messages'].includes(settings.协议 ?? 'openai_chat')) {
    problems.push({
      code: 'UNKNOWN_PROTOCOL',
      message: '协议只能是 openai_chat 或 anthropic_messages。',
    })
  }
  return problems
}

function modelTransportTone(transport: string): 'normal' | 'warning' | 'insecure' {
  if (transport.includes('明文')) {
    return 'insecure'
  }
  return transport === '未知' ? 'warning' : 'normal'
}

function ModelSettingsSection() {
  const fieldId = useId()
  const modelSettings = useApiResource(api.getModelSettings)
  const [endpoint, setEndpoint] = useState('')
  const [model, setModel] = useState('')
  const [provider, setProvider] = useState('')
  const [protocol, setProtocol] = useState<NonNullable<ModelSettingsDraft['协议']>>('openai_chat')
  const [newSecret, setNewSecret] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [requestErrors, setRequestErrors] = useState<string[] | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  useEffect(() => {
    if (modelSettings.status === 'success') {
      setEndpoint(modelSettings.data.接口地址)
      setModel(modelSettings.data.模型)
      setProvider(modelSettings.data.提供方)
      setProtocol(modelSettings.data.协议)
      setNewSecret('')
    }
  }, [modelSettings.data, modelSettings.status])

  function clearFeedback() {
    setRequestErrors(null)
    setNotice(null)
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    clearFeedback()
    const input: ModelSettingsDraft = {
      接口地址: endpoint,
      模型: model,
      提供方: provider,
      协议: protocol,
      密钥: newSecret,
    }
    const validationErrors = validateModelSettings(input)
    if (validationErrors.length > 0) {
      setRequestErrors(validationErrors.map((problem) => problem.message))
      return
    }

    setSubmitting(true)
    try {
      const response = await api.putModelSettings(input)
      if (!response.ok) {
        setRequestErrors(apiErrorMessages(response.error))
        return
      }
      setNewSecret('')
      setNotice(
        input.密钥.length > 0
          ? '模型配置已写入；密钥仍只以脱敏形式显示。'
          : '模型配置已写入；空白密钥没有覆盖现值。',
      )
      modelSettings.reload()
    } catch {
      setRequestErrors(['模型配置未写入，请检查连接后重试。'])
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section className="ledger-section runtime-section" aria-labelledby="model-settings-title">
      <header className="section-heading">
        <div>
          <h2 id="model-settings-title">模型服务</h2>
          <p>模型调用层唯一的配置来源；密钥只会返回脱敏值。</p>
        </div>
      </header>

      {modelSettings.status === 'loading' && (
        <ResourceMessage kind="loading" title="正在读取模型配置" message="密钥只会返回脱敏值。" />
      )}
      {modelSettings.status === 'error' && (
        <ResourceMessage
          kind="error"
          title="模型配置读取失败"
          message={modelSettings.error}
          apiError={modelSettings.apiError}
          onRetry={modelSettings.reload}
        />
      )}
      {modelSettings.status === 'success' && (
        <form className="runtime-form model-form" noValidate onSubmit={handleSubmit}>
          <div className="model-settings-list">
            <label className="model-setting-row" htmlFor={`${fieldId}-endpoint`}>
              <span>接口地址</span>
              <input
                id={`${fieldId}-endpoint`}
                type="text"
                required
                value={endpoint}
                onChange={(event) => {
                  setEndpoint(event.target.value)
                  clearFeedback()
                }}
              />
              <small>必须以 http:// 或 https:// 开头；明文 http 会被保留并持续标示。</small>
            </label>
            <label className="model-setting-row" htmlFor={`${fieldId}-model`}>
              <span>模型</span>
              <input
                id={`${fieldId}-model`}
                type="text"
                required
                value={model}
                onChange={(event) => {
                  setModel(event.target.value)
                  clearFeedback()
                }}
              />
              <small>这个值会原样进归档。</small>
            </label>
            <label className="model-setting-row" htmlFor={`${fieldId}-provider`}>
              <span>提供方</span>
              <input
                id={`${fieldId}-provider`}
                type="text"
                required
                value={provider}
                onChange={(event) => {
                  setProvider(event.target.value)
                  clearFeedback()
                }}
              />
              <small>换了中转就改这一栏。</small>
            </label>
            <label className="model-setting-row" htmlFor={`${fieldId}-protocol`}>
              <span>协议</span>
              <select
                id={`${fieldId}-protocol`}
                value={protocol}
                onChange={(event) => {
                  setProtocol(event.target.value as NonNullable<ModelSettingsDraft['协议']>)
                  clearFeedback()
                }}
              >
                <option value="openai_chat">openai_chat</option>
                <option value="anthropic_messages">anthropic_messages</option>
              </select>
              <small>这是线上传输格式，不是模型家族。</small>
            </label>
            <div className="model-setting-row model-secret-mask">
              <span>当前密钥</span>
              <code>{modelSettings.data.密钥}</code>
              <small>只读脱敏值</small>
            </div>
            <label className="model-setting-row" htmlFor={`${fieldId}-secret`}>
              <span>密钥</span>
              <input
                id={`${fieldId}-secret`}
                type="password"
                autoComplete="new-password"
                value={newSecret}
                placeholder="留空表示不修改"
                onChange={(event) => {
                  setNewSecret(event.target.value)
                  clearFeedback()
                }}
              />
              <small>填写后覆盖，提交后仍不回显明文。</small>
            </label>
            <div className={`model-setting-row model-transport is-${modelTransportTone(modelSettings.data.传输)}`}>
              <span>传输</span>
              <strong>{modelSettings.data.传输}</strong>
              <small>
                {modelSettings.data.传输.includes('明文')
                  ? '明文传输会让密钥与完整上下文暴露在链路上；该警示持续显示。'
                  : '后端按接口地址计算，仅供读取。'}
              </small>
            </div>
          </div>
          <div className="runtime-form-actions">
            <button type="submit" className="primary-action" disabled={submitting}>
              {submitting ? '正在保存…' : '保存模型配置'}
            </button>
          </div>
        </form>
      )}
      <MutationFeedback errors={requestErrors} notice={notice} />
    </section>
  )
}

function BrokerSettingsSection() {
  const fieldId = useId()
  const brokerSettings = useApiResource(api.getBrokerSettings)
  const [remoteEndpoint, setRemoteEndpoint] = useState('')
  const [newAccount, setNewAccount] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [requestErrors, setRequestErrors] = useState<string[] | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  useEffect(() => {
    if (brokerSettings.status === 'success') {
      setRemoteEndpoint(brokerSettings.data.浏览器远端)
      setNewAccount('')
      setNewPassword('')
    }
  }, [brokerSettings.data, brokerSettings.status])

  function clearFeedback() {
    setRequestErrors(null)
    setNotice(null)
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    clearFeedback()
    if (!remoteEndpoint.trim()) {
      setRequestErrors(['浏览器远端必须填写；系统不会猜测或补默认地址。'])
      return
    }

    const input: BrokerSettingsInput = {
      浏览器远端: remoteEndpoint.trim(),
      资金账号: newAccount.trim(),
      交易密码: newPassword,
    }
    setSubmitting(true)
    try {
      const response = await api.putBrokerSettings(input)
      if (!response.ok) {
        setRequestErrors(apiErrorMessages(response.error))
        return
      }
      setNewAccount('')
      setNewPassword('')
      setNotice('券商登录配置已写入；账号仍只显示遮罩值，密码只显示配置状态。')
      brokerSettings.reload()
    } catch {
      setRequestErrors(['券商登录配置未写入，请检查连接后重试。'])
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section className="ledger-section runtime-section" aria-labelledby="broker-settings-title">
      <header className="section-heading">
        <div>
          <h2 id="broker-settings-title">券商连接</h2>
          <p>浏览器地址不做猜测；账号和交易密码均不回显明文。</p>
        </div>
      </header>

      {brokerSettings.status === 'loading' && (
        <ResourceMessage kind="loading" title="正在读取券商配置" message="只读取遮罩账号与配置状态。" />
      )}
      {brokerSettings.status === 'error' && (
        <ResourceMessage
          kind="error"
          title="券商配置读取失败"
          message={brokerSettings.error}
          apiError={brokerSettings.apiError}
          onRetry={brokerSettings.reload}
        />
      )}
      {brokerSettings.status === 'success' && (
        <form className="runtime-form broker-form" noValidate onSubmit={handleSubmit}>
          <div className="model-settings-list broker-settings-list">
            <div className={`model-setting-row broker-status ${brokerSettings.data.已配全 ? 'is-complete' : 'is-incomplete'}`}>
              <span>配置状态</span>
              <strong>{brokerSettings.data.已配全 ? '已配全' : '未配全'}</strong>
              <small>
                {brokerSettings.data.缺项.length > 0
                  ? `还缺：${brokerSettings.data.缺项.join('、')}`
                  : '后端报告登录配置完整。'}
              </small>
            </div>
            <label className="model-setting-row" htmlFor={`${fieldId}-remote`}>
              <span>浏览器远端</span>
              <input
                id={`${fieldId}-remote`}
                type="url"
                required
                value={remoteEndpoint}
                onChange={(event) => {
                  setRemoteEndpoint(event.target.value)
                  clearFeedback()
                }}
              />
              <small>必填；只使用后端现值或你明确输入的地址。</small>
            </label>
            <div className="model-setting-row broker-account-mask">
              <span>当前资金账号</span>
              <code>{brokerSettings.data.资金账号 || '未配置'}</code>
              <small>后端返回的只读遮罩值</small>
            </div>
            <label className="model-setting-row" htmlFor={`${fieldId}-account`}>
              <span>新资金账号</span>
              <input
                id={`${fieldId}-account`}
                type="password"
                autoComplete="off"
                value={newAccount}
                placeholder="留空表示不修改"
                onChange={(event) => {
                  setNewAccount(event.target.value)
                  clearFeedback()
                }}
              />
              <small>提交后只返回遮罩值，不会再回传原值。</small>
            </label>
            <div className="model-setting-row broker-password-status">
              <span>交易密码</span>
              <strong>{brokerSettings.data.交易密码已配置 ? '已配置' : '未配置'}</strong>
              <small>后端只返回布尔状态，不返回密码字段。</small>
            </div>
            <label className="model-setting-row" htmlFor={`${fieldId}-password`}>
              <span>新交易密码</span>
              <input
                id={`${fieldId}-password`}
                type="password"
                autoComplete="new-password"
                value={newPassword}
                placeholder="留空表示不修改"
                onChange={(event) => {
                  setNewPassword(event.target.value)
                  clearFeedback()
                }}
              />
              <small>提交后只更新配置状态，不回显任何占位密码。</small>
            </label>
          </div>
          <div className="runtime-form-actions">
            <button type="submit" className="primary-action" disabled={submitting || !remoteEndpoint.trim()}>
              {submitting ? '正在保存…' : '保存券商配置'}
            </button>
          </div>
        </form>
      )}
      <MutationFeedback errors={requestErrors} notice={notice} />
    </section>
  )
}

export function RuntimePage() {
  const context = useOutletContext<AppOutletContext>()

  return (
    <section className="runtime-page page-enter">
      <PageHeader
        title="运行设置"
        description="配置自动运行、调度时间及外部服务连接。"
        aside="配置变更留痕"
      />
      <UnattendedSection systemStatus={context.systemStatus} />
      <ScheduleSection />
      <ModelSettingsSection />
      <CaptchaSection />
      <BrokerSettingsSection />
      <UsageOverview />
    </section>
  )
}
