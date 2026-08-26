import captchaFixture from '../fixtures/settings/captcha.json' with { type: 'json' }
import brokerFixture from '../fixtures/settings/broker.json' with { type: 'json' }
import modelFixture from '../fixtures/settings/model.json' with { type: 'json' }
import scheduleFixture from '../fixtures/settings/schedule.json' with { type: 'json' }
import writeOkFixture from '../fixtures/settings/write-ok.json' with { type: 'json' }
import neverSucceededStatusFixture from '../fixtures/status/never-succeeded.json' with { type: 'json' }
import statusFixture from '../fixtures/status/dry-run.json' with { type: 'json' }
import stalledStatusFixture from '../fixtures/status/stalled.json' with { type: 'json' }
import dayLowUsageFixture from '../fixtures/usage/day-low.json' with { type: 'json' }
import dayUsageFixture from '../fixtures/usage/day-normal.json' with { type: 'json' }
import emptyUsageFixture from '../fixtures/usage/empty.json' with { type: 'json' }
import modelUsageFixture from '../fixtures/usage/model-normal.json' with { type: 'json' }
import objectUsageFixture from '../fixtures/usage/object-normal.json' with { type: 'json' }
import type { ApiProblem } from './errors'
import type { ApiFailure, ApiResponse, SystemStatus } from './types'

// Usage 的返回形状仍是 fixture 视图模型；运行设置类型按当前契约维护。
export interface ScheduleSettings {
  时点: string[]
}

export interface ScheduleSettingsInput {
  时点: string[]
  原因: string
}

export type CaptchaRecognitionMethod = 'vision' | 'ttshitu' | 'chaojiying'

export interface CaptchaRecognizerSettings {
  接口地址: string
  模型: string
  识别方式: CaptchaRecognitionMethod
  密钥: string
}

export interface CaptchaRecognizerInput {
  接口地址: string
  模型: string
  识别方式: CaptchaRecognitionMethod
  密钥: string
}

export interface CaptchaSettings extends CaptchaRecognizerSettings {
  备用识别: CaptchaRecognizerSettings[]
}

export interface CaptchaSettingsInput extends CaptchaRecognizerInput {
  备用识别: CaptchaRecognizerInput[]
}

export interface BrokerSettings {
  浏览器远端: string
  资金账号: string
  交易密码已配置: boolean
  缺项: string[]
  已配全: boolean
}

export interface BrokerSettingsInput {
  浏览器远端: string
  资金账号: string
  交易密码: string
}

export type ModelProtocol = 'openai_chat' | 'anthropic_messages'

export type ModelTransport =
  | 'https'
  | 'http(本机回环,不出网卡)'
  | '明文 http(密钥与整份上下文会摊在链路上)'
  | '未知'

export interface ModelSettings {
  接口地址: string
  模型: string
  提供方: string
  协议: ModelProtocol
  密钥: string
  传输: ModelTransport
}

export interface ModelSettingsDraft {
  接口地址: string
  模型: string
  提供方: string
  协议?: ModelProtocol
  密钥: string
}

export interface UnattendedSettingsInput {
  无人值守: boolean
  原因: string
}

export type UsageGroupBy = 'day' | 'object' | 'model'

export interface UsageQuery {
  from?: string
  to?: string
  group_by: UsageGroupBy
}

interface UsageMetrics {
  轮数: number
  input_tokens: number
  output_tokens: number
  reasoning_tokens: number
  cached_tokens: number
  缓存命中率: number
}

export type UsageDayRow = UsageMetrics & { 日期: string }
export type UsageObjectRow = UsageMetrics & { object_id: string }
export type UsageModelRow = UsageMetrics & { model: string }
export type UsageRow = UsageDayRow | UsageObjectRow | UsageModelRow

export interface RuntimeApi {
  getSchedule(): Promise<ApiResponse<ScheduleSettings>>
  putSchedule(settings: ScheduleSettingsInput): Promise<ApiResponse<Record<string, never>>>
  getCaptcha(): Promise<ApiResponse<CaptchaSettings>>
  putCaptcha(settings: CaptchaSettingsInput): Promise<ApiResponse<Record<string, never>>>
  getModelSettings(): Promise<ApiResponse<ModelSettings>>
  putModelSettings(settings: ModelSettingsDraft): Promise<ApiResponse<unknown>>
  getBrokerSettings(): Promise<ApiResponse<BrokerSettings>>
  putBrokerSettings(settings: BrokerSettingsInput): Promise<ApiResponse<Record<string, never>>>
  putUnattended(settings: UnattendedSettingsInput): Promise<ApiResponse<Record<string, never>>>
  getUsage(query: UsageQuery): Promise<ApiResponse<UsageRow[]>>
}

export const runtimeEndpoints = {
  schedule: '/api/settings/schedule',
  captcha: '/api/settings/captcha',
  model: '/api/settings/model',
  broker: '/api/settings/broker',
  unattended: '/api/settings/unattended',
  usage: '/api/usage',
} as const

export type RuntimeFixtureScenario =
  | 'default'
  | 'usage-low'
  | 'usage-empty'
  | 'runtime-error'
  | 'runtime-write-error'
  | 'stalled'
  | 'never-success'

interface RuntimeFixtureOptions {
  // 仅供 fixture 校验模拟首次保存前没有已存密钥的状态。
  initialModelSecretConfigured?: boolean
}

const fixtureScenarios = new Set<RuntimeFixtureScenario>([
  'default',
  'usage-low',
  'usage-empty',
  'runtime-error',
  'runtime-write-error',
  'stalled',
  'never-success',
])

const aggregateFixtureWindow = {
  from: '2026-08-14',
  to: '2026-08-18',
} as const

const runtimeReadFailure: ApiFailure = {
  ok: false,
  error: {
    code: 'RUNTIME_FIXTURE_READ_FAILED',
    message: '运行配置样例读取失败，请重试。',
  },
}

const runtimeWriteFailure: ApiFailure = {
  ok: false,
  error: {
    code: 'RUNTIME_FIXTURE_WRITE_FAILED',
    message: '配置未写入，样例层返回了明确失败。',
  },
}

function getRuntimeFixtureScenario(): RuntimeFixtureScenario {
  if (typeof window === 'undefined') {
    return 'default'
  }

  const scenario = new URLSearchParams(window.location.search).get('fixture')
  if (scenario === 'error') {
    return 'runtime-error'
  }
  if (scenario === 'mutation-error') {
    return 'runtime-write-error'
  }
  return fixtureScenarios.has(scenario as RuntimeFixtureScenario)
    ? (scenario as RuntimeFixtureScenario)
    : 'default'
}

function cloneResponse<T>(response: ApiResponse<T>): Promise<ApiResponse<T>> {
  return Promise.resolve(structuredClone(response))
}

function writeSuccess(): Promise<ApiResponse<Record<string, never>>> {
  return cloneResponse(
    writeOkFixture as unknown as ApiResponse<Record<string, never>>,
  )
}

function failure(
  code: string,
  message: string,
  problems: ApiProblem[] = [],
): Promise<ApiFailure> {
  const response: ApiFailure = {
    ok: false,
    error: problems.length > 0
      ? { code, message, 问题: problems }
      : { code, message },
  }
  return Promise.resolve(structuredClone(response))
}

function scheduleProblems(settings: ScheduleSettingsInput): ApiProblem[] {
  const problems: ApiProblem[] = []
  const rawTimes: unknown = settings.时点
  const times = Array.isArray(rawTimes)
    ? rawTimes.filter((time): time is string => typeof time === 'string')
    : []
  const allTimesAreStrings = Array.isArray(rawTimes) && times.length === rawTimes.length
  const allTimesHaveValidFormat =
    allTimesAreStrings && times.every((time) => /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(time))

  if (!Array.isArray(rawTimes) || rawTimes.length !== 6) {
    problems.push({ code: 'INVALID_SCHEDULE', message: '调度计划必须恰好包含六个时点。' })
  }
  if (!allTimesHaveValidFormat) {
    problems.push({ code: 'INVALID_SCHEDULE', message: '每个调度时点都必须是合法的 HH:MM。' })
  }
  if (allTimesAreStrings && new Set(times).size !== times.length) {
    problems.push({ code: 'INVALID_SCHEDULE', message: '六个调度时点不能重复。' })
  }
  if (
    allTimesHaveValidFormat &&
    times.some((time, index) => index > 0 && time <= (times[index - 1] ?? ''))
  ) {
    problems.push({ code: 'INVALID_SCHEDULE', message: '六个调度时点必须按时间升序排列。' })
  }

  const reason: unknown = settings.原因
  if (typeof reason !== 'string' || reason.trim().length === 0) {
    problems.push({ code: 'REASON_REQUIRED', message: '修改调度计划必须填写原因。' })
  }

  return problems
}

function maskSecret(secret: string): string {
  return `sk-****${secret.slice(-4).padStart(4, '*')}`
}

function maskModelSecret(secret: string): string {
  return `****${secret.slice(-4).padStart(4, '*')}`
}

function maskBrokerAccount(account: string): string {
  const normalized = account.trim()
  return normalized.length > 7
    ? `${normalized.slice(0, 3)}****${normalized.slice(-4)}`
    : `****${normalized.slice(-4).padStart(4, '*')}`
}

function modelTransport(endpoint: string): ModelTransport {
  try {
    const url = new URL(endpoint)
    if (url.protocol === 'https:') {
      return 'https'
    }
    if (url.protocol !== 'http:') {
      return '未知'
    }

    const host = url.hostname.toLowerCase()
    if (host === 'localhost' || host === '::1' || host === '[::1]' || /^127(?:\.\d{1,3}){3}$/.test(host)) {
      return 'http(本机回环,不出网卡)'
    }
    return '明文 http(密钥与整份上下文会摊在链路上)'
  } catch {
    return '未知'
  }
}

function modelProblems(
  settings: unknown,
  hasConfiguredSecret: boolean,
): ApiProblem[] {
  const problems: ApiProblem[] = []
  const input = settings !== null && typeof settings === 'object'
    ? settings as Record<string, unknown>
    : {}
  const endpoint = input.接口地址
  const model = input.模型
  const provider = input.提供方
  const protocol = input.协议
  const secret = input.密钥

  if (
    typeof endpoint !== 'string' ||
    (!endpoint.startsWith('http://') && !endpoint.startsWith('https://'))
  ) {
    problems.push({
      code: 'ENDPOINT_SCHEME',
      message: '接口地址必须以 http:// 或 https:// 开头。',
    })
  }
  if (typeof model !== 'string' || !model.trim()) {
    problems.push({ code: 'MODEL_REQUIRED', message: '模型不能为空。' })
  }
  if (typeof provider !== 'string' || !provider.trim()) {
    problems.push({ code: 'PROVIDER_REQUIRED', message: '提供方不能为空。' })
  }
  if (protocol !== undefined && protocol !== 'openai_chat' && protocol !== 'anthropic_messages') {
    problems.push({ code: 'UNKNOWN_PROTOCOL', message: '协议只能是 openai_chat 或 anthropic_messages。' })
  }
  if (typeof secret !== 'string' || (!hasConfiguredSecret && !secret.trim())) {
    problems.push({
      code: 'SECRET_REQUIRED',
      message: hasConfiguredSecret
        ? '密钥必须是字符串；留空表示不修改。'
        : '首次配置模型接口必须填写密钥。',
    })
  }
  return problems
}

function brokerProblems(settings: unknown): ApiProblem[] {
  const input = settings !== null && typeof settings === 'object'
    ? settings as Record<string, unknown>
    : {}
  const endpoint = input.浏览器远端
  const account = input.资金账号
  const password = input.交易密码
  const problems: ApiProblem[] = []

  if (typeof endpoint !== 'string' || !endpoint.trim()) {
    problems.push({ code: 'INVALID_BROKER_SETTINGS', message: '浏览器远端不能为空。' })
  }
  if (typeof account !== 'string') {
    problems.push({ code: 'INVALID_BROKER_SETTINGS', message: '资金账号必须是字符串，留空表示不修改。' })
  }
  if (typeof password !== 'string') {
    problems.push({ code: 'INVALID_BROKER_SETTINGS', message: '交易密码必须是字符串，留空表示不修改。' })
  }
  return problems
}

function selectUsageFixture(
  scenario: RuntimeFixtureScenario,
  groupBy: UsageGroupBy,
): ApiResponse<UsageRow[]> {
  if (scenario === 'usage-empty') {
    return emptyUsageFixture as unknown as ApiResponse<UsageRow[]>
  }
  if (scenario === 'usage-low' && groupBy === 'day') {
    return dayLowUsageFixture as unknown as ApiResponse<UsageRow[]>
  }
  if (groupBy === 'object') {
    return objectUsageFixture as unknown as ApiResponse<UsageRow[]>
  }
  if (groupBy === 'model') {
    return modelUsageFixture as unknown as ApiResponse<UsageRow[]>
  }
  return dayUsageFixture as unknown as ApiResponse<UsageRow[]>
}

function filterUsageFixture(rows: UsageRow[], query: UsageQuery): UsageRow[] {
  if (query.group_by !== 'day') {
    const coversFixtureWindow =
      (!query.from || query.from <= aggregateFixtureWindow.from) &&
      (!query.to || query.to >= aggregateFixtureWindow.to)
    return coversFixtureWindow ? rows : []
  }

  return rows.filter((row) => {
    if (!('日期' in row)) {
      return false
    }
    return (!query.from || row.日期 >= query.from) && (!query.to || row.日期 <= query.to)
  })
}

export function createRuntimeFixtureApi(
  resolveScenario: () => RuntimeFixtureScenario = getRuntimeFixtureScenario,
  options: RuntimeFixtureOptions = {},
): RuntimeApi {
  const initialSchedule = scheduleFixture as unknown as ApiResponse<ScheduleSettings>
  const initialCaptcha = captchaFixture as unknown as ApiResponse<CaptchaSettings>
  const initialBroker = brokerFixture as unknown as ApiResponse<BrokerSettings>
  const initialModel = modelFixture as unknown as ApiResponse<ModelSettings>
  let scheduleState = initialSchedule.ok
    ? structuredClone(initialSchedule.data)
    : { 时点: [] }
  let captchaState: CaptchaSettings = initialCaptcha.ok
    ? structuredClone(initialCaptcha.data)
    : { 接口地址: '', 模型: '', 识别方式: 'vision', 密钥: '', 备用识别: [] }
  let brokerState = initialBroker.ok
    ? structuredClone(initialBroker.data)
    : {
        浏览器远端: '',
        资金账号: '',
        交易密码已配置: false,
        缺项: ['浏览器远端', '资金账号', '交易密码'],
        已配全: false,
      }
  let modelState = initialModel.ok
    ? structuredClone(initialModel.data)
    : {
        接口地址: '',
        模型: '',
        提供方: '',
        协议: 'openai_chat' as const,
        密钥: '********',
        传输: '未知' as const,
      }
  let modelSecretConfigured = options.initialModelSecretConfigured ?? true

  return {
    getSchedule: () =>
      resolveScenario() === 'runtime-error'
        ? cloneResponse(runtimeReadFailure)
        : cloneResponse({ ok: true, data: scheduleState }),

    putSchedule: (settings) => {
      const problems = scheduleProblems(settings)
      if (problems.length > 0) {
        return failure(
          'INVALID_SCHEDULE',
          problems.map((problem) => problem.message).join('；'),
          problems,
        )
      }
      if (resolveScenario() === 'runtime-write-error') {
        return cloneResponse(runtimeWriteFailure)
      }

      scheduleState = { 时点: [...settings.时点] }
      return writeSuccess()
    },

    getCaptcha: () =>
      resolveScenario() === 'runtime-error'
        ? cloneResponse(runtimeReadFailure)
        : cloneResponse({ ok: true, data: captchaState }),

    putCaptcha: (settings) => {
      const problems: ApiProblem[] = []
      const methods: CaptchaRecognitionMethod[] = ['vision', 'ttshitu', 'chaojiying']
      const validateRecognizer = (
        recognizer: CaptchaRecognizerInput | undefined,
        label: string,
        current: CaptchaRecognizerSettings | undefined,
        requireIdentityMatch: boolean,
      ) => {
        if (!recognizer || typeof recognizer !== 'object') {
          problems.push({ code: 'INVALID_CAPTCHA_SETTINGS', message: `${label}必须是对象。` })
          return
        }
        if (typeof recognizer.接口地址 !== 'string' || !recognizer.接口地址.trim()) {
          problems.push({ code: 'INVALID_CAPTCHA_SETTINGS', message: `${label}接口地址不能为空。` })
        }
        if (!methods.includes(recognizer.识别方式)) {
          problems.push({ code: 'INVALID_CAPTCHA_SETTINGS', message: `${label}识别方式不受支持。` })
        }
        if (recognizer.识别方式 === 'vision' && (typeof recognizer.模型 !== 'string' || !recognizer.模型.trim())) {
          problems.push({ code: 'INVALID_CAPTCHA_SETTINGS', message: `${label}使用视觉模型时必须填写模型。` })
        } else if (typeof recognizer.模型 !== 'string') {
          problems.push({ code: 'INVALID_CAPTCHA_SETTINGS', message: `${label}模型必须是字符串。` })
        }
        if (typeof recognizer.密钥 !== 'string') {
          problems.push({ code: 'INVALID_CAPTCHA_SETTINGS', message: `${label}密钥必须是字符串。` })
          return
        }

        const sameRecognizer = current
          && current.接口地址 === recognizer.接口地址.trim()
          && current.模型 === recognizer.模型.trim()
          && current.识别方式 === recognizer.识别方式
        if (!recognizer.密钥.trim() && (!current?.密钥 || (requireIdentityMatch && !sameRecognizer))) {
          problems.push({ code: 'INVALID_CAPTCHA_SETTINGS', message: `${label}是新配置，必须填写密钥。` })
        }
      }

      validateRecognizer(settings, '主识别服务', captchaState, false)
      if (!Array.isArray(settings.备用识别)) {
        problems.push({ code: 'INVALID_CAPTCHA_SETTINGS', message: '备用识别必须是数组。' })
      } else {
        settings.备用识别.forEach((recognizer, index) => {
          validateRecognizer(recognizer, `备用识别第 ${index + 1} 条`, captchaState.备用识别[index], true)
        })
      }
      if (problems.length > 0) {
        return failure(
          'INVALID_CAPTCHA_SETTINGS',
          problems.map((problem) => problem.message).join('；'),
          problems,
        )
      }
      if (resolveScenario() === 'runtime-write-error') {
        return cloneResponse(runtimeWriteFailure)
      }

      const nextSecret = settings.密钥.trim()
      const nextBackups = settings.备用识别.length === 0
        ? captchaState.备用识别
        : settings.备用识别.map((recognizer, index) => ({
            接口地址: recognizer.接口地址.trim(),
            模型: recognizer.模型.trim(),
            识别方式: recognizer.识别方式,
            密钥: recognizer.密钥.trim()
              ? maskSecret(recognizer.密钥.trim())
              : captchaState.备用识别[index]?.密钥 ?? '',
          }))
      captchaState = {
        接口地址: settings.接口地址.trim(),
        模型: settings.模型.trim(),
        识别方式: settings.识别方式,
        密钥: nextSecret ? maskSecret(nextSecret) : captchaState.密钥,
        备用识别: nextBackups,
      }
      return writeSuccess()
    },

    getModelSettings: () =>
      resolveScenario() === 'runtime-error'
        ? cloneResponse(runtimeReadFailure)
        : cloneResponse({ ok: true, data: modelState }),

    putModelSettings: (settings) => {
      const problems = modelProblems(settings, modelSecretConfigured)
      if (problems.length > 0) {
        return failure(
          'INVALID_MODEL_SETTINGS',
          problems.map((problem) => problem.message).join('；'),
          problems,
        )
      }
      if (resolveScenario() === 'runtime-write-error') {
        return cloneResponse(runtimeWriteFailure)
      }

      const nextSecret = settings.密钥.trim()
      modelState = {
        接口地址: settings.接口地址,
        模型: settings.模型,
        提供方: settings.提供方,
        协议: settings.协议 ?? 'openai_chat',
        密钥: nextSecret ? maskModelSecret(nextSecret) : modelState.密钥,
        传输: modelTransport(settings.接口地址),
      }
      modelSecretConfigured ||= nextSecret.length > 0
      return writeSuccess()
    },

    getBrokerSettings: () =>
      resolveScenario() === 'runtime-error'
        ? cloneResponse(runtimeReadFailure)
        : cloneResponse({ ok: true, data: brokerState }),

    putBrokerSettings: (settings) => {
      const problems = brokerProblems(settings)
      if (problems.length > 0) {
        return failure(
          'INVALID_BROKER_SETTINGS',
          problems.map((problem) => problem.message).join('；'),
          problems,
        )
      }
      if (resolveScenario() === 'runtime-write-error') {
        return cloneResponse(runtimeWriteFailure)
      }

      const validSettings = settings as BrokerSettingsInput
      const nextAccount = validSettings.资金账号.trim()
      const accountMask = nextAccount ? maskBrokerAccount(nextAccount) : brokerState.资金账号
      const passwordConfigured = validSettings.交易密码.length > 0 || brokerState.交易密码已配置
      const missing = [
        ...(accountMask ? [] : ['资金账号']),
        ...(passwordConfigured ? [] : ['交易密码']),
      ]
      brokerState = {
        浏览器远端: validSettings.浏览器远端.trim(),
        资金账号: accountMask,
        交易密码已配置: passwordConfigured,
        缺项: missing,
        已配全: missing.length === 0,
      }
      return writeSuccess()
    },

    putUnattended: (settings) => {
      const problems: ApiProblem[] = []
      if (typeof settings.无人值守 !== 'boolean') {
        problems.push({ code: 'INVALID_UNATTENDED', message: '无人值守必须是布尔值。' })
      }
      if (typeof settings.原因 !== 'string' || !settings.原因.trim()) {
        problems.push({ code: 'REASON_REQUIRED', message: '变更无人值守模式必须填写原因。' })
      }
      if (problems.length > 0) {
        return failure(
          'INVALID_UNATTENDED',
          problems.map((problem) => problem.message).join('；'),
          problems,
        )
      }
      const scenario = resolveScenario()
      if (scenario === 'runtime-write-error') {
        return cloneResponse(runtimeWriteFailure)
      }

      const selectedStatusFixture =
        scenario === 'stalled'
          ? stalledStatusFixture
          : scenario === 'never-success'
            ? neverSucceededStatusFixture
            : statusFixture
      const mutableStatus = selectedStatusFixture as unknown as ApiResponse<SystemStatus>
      if (mutableStatus.ok) {
        mutableStatus.data.无人值守 = settings.无人值守
      }
      return writeSuccess()
    },

    getUsage: (query) => {
      const scenario = resolveScenario()
      if (scenario === 'runtime-error') {
        return cloneResponse(runtimeReadFailure)
      }
      if (!['day', 'object', 'model'].includes(query.group_by)) {
        return failure('INVALID_USAGE_GROUP', '用量分组只支持 day、object 或 model。')
      }
      if (query.from && query.to && query.from > query.to) {
        return failure('INVALID_USAGE_RANGE', '用量查询的开始日期不能晚于结束日期。')
      }

      const response = structuredClone(selectUsageFixture(scenario, query.group_by))
      return response.ok
        ? cloneResponse({ ok: true, data: filterUsageFixture(response.data, query) })
        : cloneResponse(response)
    },
  }
}

export const runtimeApi = createRuntimeFixtureApi()
