import type { ApiClient } from './client'
import type {
  AccountSummary,
  ApiFailure,
  ApiResponse,
  CompareRunsParams,
  Instruction,
  RunComparison,
  RunSummary,
  StrategyRun,
  SystemStatus,
  TradeObject,
  TradeObjectDraft,
} from './types'
import type {
  BrokerSettings,
  BrokerSettingsInput,
  CaptchaSettings,
  CaptchaSettingsInput,
  ModelSettings,
  ModelSettingsDraft,
  ScheduleSettings,
  ScheduleSettingsInput,
  UnattendedSettingsInput,
  UsageQuery,
  UsageRow,
} from './types.runtime.ts'

const REQUEST_TIMEOUT_MS = 15_000

type HttpMethod = 'GET' | 'POST' | 'PUT' | 'DELETE'

type RequestOutcome<T> = {
  response: ApiResponse<T>
  retryable: boolean
}

function failure(code: string, message: string): ApiFailure {
  return { ok: false, error: { code, message } }
}

function timeoutOutcome<T>(): RequestOutcome<T> {
  return {
    response: failure(
      'REQUEST_TIMEOUT',
      '请求超时，未能在规定时间内收到后端响应。',
    ),
    retryable: true,
  }
}

function readJsonUntilAbort(response: Response, signal: AbortSignal): Promise<unknown> {
  return new Promise((resolve, reject) => {
    let settled = false
    const finish = (callback: () => void) => {
      if (settled) {
        return
      }
      settled = true
      signal.removeEventListener('abort', onAbort)
      callback()
    }
    const onAbort = () => finish(() => reject(new Error('request aborted while reading JSON')))

    if (signal.aborted) {
      onAbort()
      return
    }

    signal.addEventListener('abort', onAbort, { once: true })
    void response.json().then(
      (payload) => finish(() => resolve(payload)),
      (error: unknown) => finish(() => reject(error)),
    )
  })
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function isApiProblem(value: unknown): boolean {
  return (
    isRecord(value) &&
    typeof value.code === 'string' &&
    value.code.length > 0 &&
    typeof value.message === 'string' &&
    value.message.length > 0
  )
}

function isApiFailure(value: unknown): value is ApiFailure {
  if (!(
    isRecord(value) &&
    value.ok === false &&
    isRecord(value.error) &&
    isApiProblem(value.error)
  )) {
    return false
  }

  return (
    !Object.hasOwn(value.error, '问题') ||
    (Array.isArray(value.error.问题) && value.error.问题.every(isApiProblem))
  )
}

function isApiResponse<T>(value: unknown): value is ApiResponse<T> {
  if (!isRecord(value)) {
    return false
  }

  if (value.ok === true) {
    return Object.hasOwn(value, 'data')
  }

  return isApiFailure(value)
}

function endpoint(baseUrl: string, path: string): string {
  return `${baseUrl.replace(/\/+$/, '')}${path}`
}

function withQuery(
  path: string,
  params: Record<string, string | number | undefined>,
): string {
  const query = new URLSearchParams()

  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) {
      query.set(key, String(value))
    }
  }

  const search = query.toString()
  return search ? `${path}?${search}` : path
}

function objectPayload(draft: TradeObjectDraft): TradeObjectDraft {
  return {
    market: draft.market,
    symbol: draft.symbol,
    名称: draft.名称,
    类型: draft.类型,
    资产类型: draft.资产类型,
  }
}

async function requestOnce<T>(
  url: string,
  method: HttpMethod,
  body: string | undefined,
): Promise<RequestOutcome<T>> {
  const controller = new AbortController()
  const timeoutId = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)

  try {
    const init: RequestInit = { method, signal: controller.signal }
    if (body !== undefined) {
      init.headers = { 'Content-Type': 'application/json' }
      init.body = body
    }

    const response = await fetch(url, init)
    let payload: unknown
    try {
      payload = await readJsonUntilAbort(response, controller.signal)
    } catch {
      if (controller.signal.aborted) {
        return timeoutOutcome<T>()
      }
      if (response.status !== 200) {
        return {
          response: failure(
            'HTTP_ERROR',
            `后端服务返回 HTTP ${response.status}，本次请求未能完成。`,
          ),
          retryable: response.status >= 500,
        }
      }

      return {
        response: failure(
          'INVALID_JSON_RESPONSE',
          '后端返回的内容不是有效 JSON，无法处理本次请求。',
        ),
        retryable: true,
      }
    }

    if (response.status !== 200) {
      const apiFailure = isApiFailure(payload) ? payload : null
      return {
        response: apiFailure
          ? apiFailure
          : failure(
              'HTTP_ERROR',
              `后端服务返回 HTTP ${response.status}，本次请求未能完成。`,
            ),
        retryable: apiFailure === null && response.status >= 500,
      }
    }

    if (!isApiResponse<T>(payload)) {
      return {
        response: failure(
          'INVALID_RESPONSE',
          '后端返回的数据不符合统一响应包契约。',
        ),
        retryable: true,
      }
    }

    return { response: payload, retryable: false }
  } catch {
    return controller.signal.aborted
      ? timeoutOutcome<T>()
      : {
          response: failure(
            'NETWORK_ERROR',
            '无法连接后端服务，请检查网络连接后重试。',
          ),
          retryable: true,
        }
  } finally {
    clearTimeout(timeoutId)
  }
}

async function request<T>(
  baseUrl: string,
  path: string,
  method: HttpMethod = 'GET',
  payload?: unknown,
): Promise<ApiResponse<T>> {
  let body: string | undefined
  try {
    body = payload === undefined ? undefined : JSON.stringify(payload)
  } catch {
    return failure(
      'REQUEST_SERIALIZATION_ERROR',
      '请求数据无法序列化，本次请求未发送到后端。',
    )
  }

  const first = await requestOnce<T>(endpoint(baseUrl, path), method, body)
  if (method !== 'GET' || !first.retryable) {
    return first.response
  }

  const second = await requestOnce<T>(endpoint(baseUrl, path), method, body)
  return second.response
}

export function createHttpClient(baseUrl: string): ApiClient {
  return {
    getStatus: () => request<SystemStatus>(baseUrl, '/api/status'),

    getObjects: () => request<TradeObject[]>(baseUrl, '/api/objects'),

    // 写操作绝不重试：超时不代表服务端未执行，自动重试可能造成重复下单。
    createObject: (draft) =>
      request<unknown>(baseUrl, '/api/objects', 'POST', objectPayload(draft)),

    updateObject: (objectId, draft) =>
      request<unknown>(
        baseUrl,
        `/api/objects/${encodeURIComponent(objectId)}`,
        'PUT',
        objectPayload(draft),
      ),

    deleteObject: (objectId) =>
      request<unknown>(
        baseUrl,
        `/api/objects/${encodeURIComponent(objectId)}`,
        'DELETE',
      ),

    getAccount: () => request<AccountSummary>(baseUrl, '/api/account'),

    getRuns: (params = {}) =>
      request<RunSummary[]>(
        baseUrl,
        withQuery('/api/runs', {
          limit: params.limit,
          from: params.from,
          to: params.to,
          system_name: params.system_name,
        }),
      ),

    getRun: (strategyId) =>
      request<StrategyRun>(
        baseUrl,
        `/api/runs/${encodeURIComponent(strategyId)}`,
      ),

    compareRuns: (params: CompareRunsParams = {}) =>
      request<RunComparison>(
        baseUrl,
        withQuery('/api/runs/compare', {
          from: params.from,
          to: params.to,
        }),
      ),

    getUsage: (query: UsageQuery) =>
      request<UsageRow[]>(
        baseUrl,
        withQuery('/api/usage', {
          from: query.from,
          to: query.to,
          group_by: query.group_by,
        }),
      ),

    getPendingInstructions: () =>
      request<Instruction[]>(baseUrl, '/api/instructions/pending'),

    confirmInstruction: (code) =>
      request<never>(
        baseUrl,
        `/api/instructions/${encodeURIComponent(code)}/confirm`,
        'POST',
      ),

    getSchedule: () =>
      request<ScheduleSettings>(baseUrl, '/api/settings/schedule'),

    putSchedule: (settings: ScheduleSettingsInput) =>
      request<Record<string, never>>(
        baseUrl,
        '/api/settings/schedule',
        'PUT',
        {
          时点: settings.时点,
          原因: settings.原因,
        },
      ),

    getCaptcha: () =>
      request<CaptchaSettings>(baseUrl, '/api/settings/captcha'),

    putCaptcha: (settings: CaptchaSettingsInput) =>
      request<Record<string, never>>(
        baseUrl,
        '/api/settings/captcha',
        'PUT',
        {
          接口地址: settings.接口地址,
          模型: settings.模型,
          识别方式: settings.识别方式,
          密钥: settings.密钥,
          备用识别: settings.备用识别.map((recognizer) => ({
            接口地址: recognizer.接口地址,
            模型: recognizer.模型,
            识别方式: recognizer.识别方式,
            密钥: recognizer.密钥,
          })),
        },
      ),

    getModelSettings: () =>
      request<ModelSettings>(baseUrl, '/api/settings/model'),

    putModelSettings: (settings: ModelSettingsDraft) =>
      request<unknown>(
        baseUrl,
        '/api/settings/model',
        'PUT',
        {
          接口地址: settings.接口地址,
          模型: settings.模型,
          提供方: settings.提供方,
          ...(settings.协议 === undefined ? {} : { 协议: settings.协议 }),
          密钥: settings.密钥,
        },
      ),

    getBrokerSettings: () =>
      request<BrokerSettings>(baseUrl, '/api/settings/broker'),

    putBrokerSettings: (settings: BrokerSettingsInput) =>
      request<Record<string, never>>(
        baseUrl,
        '/api/settings/broker',
        'PUT',
        {
          浏览器远端: settings.浏览器远端,
          资金账号: settings.资金账号,
          交易密码: settings.交易密码,
        },
      ),

    putUnattended: (settings: UnattendedSettingsInput) =>
      request<Record<string, never>>(
        baseUrl,
        '/api/settings/unattended',
        'PUT',
        {
          无人值守: settings.无人值守,
          原因: settings.原因,
        },
      ),
  }
}
