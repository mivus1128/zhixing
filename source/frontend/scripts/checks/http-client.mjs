import { createHttpClient } from '../../src/api/httpClient.ts'
import { apiErrorMessages, apiErrorPresentation } from '../../src/api/errors.ts'

function assert(condition, message) {
  if (!condition) {
    throw new Error(message)
  }
}

function jsonResponse(status, payload) {
  return {
    status,
    json: async () => structuredClone(payload),
  }
}

function assertFailure(response, code, message) {
  assert(response.ok === false, message)
  if (!response.ok) {
    assert(response.error.code === code, `${message}：错误码必须是 ${code}`)
    assert(response.error.message.length > 0, `${message}：必须提供面向人的完整说明`)
  }
}

async function drainMicrotasks() {
  await Promise.resolve()
  await Promise.resolve()
  await Promise.resolve()
  await Promise.resolve()
}

const originalFetch = globalThis.fetch
const originalSetTimeout = globalThis.setTimeout

try {
  const client = createHttpClient('https://backend.example.invalid/')
  const calls = []

  globalThis.fetch = async (url, init) => {
    calls.push({ url, init })
    return jsonResponse(200, { ok: true, data: { source: 'stub' } })
  }

  const normal = await client.getStatus()
  assert(normal.ok === true && normal.data.source === 'stub', '200 ok:true 必须正常解包')
  assert(calls[0]?.url === 'https://backend.example.invalid/api/status', '状态接口路径必须符合契约')

  const expectedFailure = {
    ok: false,
    error: { code: 'NOT_FOUND', message: '当前资源不存在。' },
  }
  globalThis.fetch = async () => jsonResponse(200, expectedFailure)
  const passthrough = await client.getObjects()
  assert(
    JSON.stringify(passthrough) === JSON.stringify(expectedFailure),
    '200 ok:false 必须原样透传统一响应包',
  )

  const noSuchEndpoint = apiErrorPresentation({
    code: 'NO_SUCH_ENDPOINT',
    message: '演示路由不存在。',
  })
  assert(
    noSuchEndpoint.kind === 'error' && noSuchEndpoint.message.includes('前后端版本不匹配'),
    'NO_SUCH_ENDPOINT 必须明确为前后端路由版本不匹配',
  )
  assert(
    apiErrorPresentation(expectedFailure.error).message === expectedFailure.error.message,
    'NOT_FOUND 必须保留接口存在但记录缺失的原始说明',
  )
  const dryRunLocked = apiErrorPresentation({
    code: 'DRY_RUN_LOCKED',
    message: '演示锁定说明。',
  })
  assert(
    dryRunLocked.kind === 'notice' && dryRunLocked.message.includes('只读演练态'),
    'DRY_RUN_LOCKED 必须作为正常只读状态呈现',
  )
  const orderPathIncomplete = apiErrorPresentation({
    code: 'ORDER_PATH_INCOMPLETE',
    message: '演示动态缺项说明。',
  })
  assert(
    orderPathIncomplete.kind === 'notice' && orderPathIncomplete.message === '演示动态缺项说明。',
    'ORDER_PATH_INCOMPLETE 必须作为提示并原样保留后端动态说明',
  )
  assert(
    JSON.stringify(apiErrorMessages({
      code: 'INVALID_MODEL_SETTINGS',
      message: '模型配置有多项问题。',
      问题: [
        { code: 'ENDPOINT_SCHEME', message: '地址协议不合法。' },
        { code: 'MODEL_REQUIRED', message: '模型不能为空。' },
      ],
    })) === JSON.stringify(['地址协议不合法。', '模型不能为空。']),
    'error.问题[] 必须一次完整保留，不得只显示第一条',
  )

  let serverErrorCalls = 0
  globalThis.fetch = async () => {
    serverErrorCalls += 1
    return jsonResponse(500, { detail: 'unexpected' })
  }
  assertFailure(await client.getAccount(), 'HTTP_ERROR', '500 必须转换成统一失败响应')
  assert(serverErrorCalls === 2, 'GET 遇到 500 时只允许重试一次')

  let networkCalls = 0
  globalThis.fetch = async () => {
    networkCalls += 1
    throw new Error('network unavailable')
  }
  assertFailure(await client.getStatus(), 'NETWORK_ERROR', '网络异常不得抛出到组件')
  assert(networkCalls === 2, 'GET 网络异常时只允许重试一次')

  let timeoutCalls = 0
  globalThis.setTimeout = (callback) => {
    callback()
    return 0
  }
  globalThis.fetch = async (_url, init) => {
    timeoutCalls += 1
    assert(init?.signal?.aborted === true, '超时必须通过 AbortController 中止请求')
    throw new Error('request aborted')
  }
  assertFailure(await client.getStatus(), 'REQUEST_TIMEOUT', '超时不得抛出到组件')
  assert(timeoutCalls === 2, 'GET 超时时只允许重试一次')
  globalThis.setTimeout = originalSetTimeout

  const bodyTimeoutCallbacks = []
  const bodyTimeoutSignals = []
  globalThis.setTimeout = (callback) => {
    bodyTimeoutCallbacks.push(callback)
    return bodyTimeoutCallbacks.length
  }
  globalThis.fetch = async (_url, init) => {
    bodyTimeoutSignals.push(init?.signal)
    return {
      status: 200,
      // 模拟响应头已经到达、但 body 读取忽略 abort 且永不结束的极端情况。
      json: async () => new Promise(() => {}),
    }
  }
  const bodyTimeoutResponse = client.getStatus()
  for (let attempt = 0; attempt < 2; attempt += 1) {
    await drainMicrotasks()
    const expire = bodyTimeoutCallbacks.shift()
    assert(typeof expire === 'function', '读取 JSON 时必须保留超时计时器')
    expire()
  }
  assertFailure(
    await bodyTimeoutResponse,
    'REQUEST_TIMEOUT',
    '响应 body 读取超时不得误报为 JSON 错误或永久挂起',
  )
  assert(
    bodyTimeoutSignals.length === 2 && bodyTimeoutSignals.every((signal) => signal?.aborted),
    '响应 body 超时必须中止两次 GET 尝试',
  )
  globalThis.setTimeout = originalSetTimeout

  let nonJsonCalls = 0
  globalThis.fetch = async () => {
    nonJsonCalls += 1
    return {
      status: 200,
      json: async () => {
        throw new SyntaxError('not json')
      },
    }
  }
  assertFailure(
    await client.getStatus(),
    'INVALID_JSON_RESPONSE',
    '非 JSON 响应体必须转换成统一失败响应',
  )
  assert(nonJsonCalls === 2, 'GET 非 JSON 响应时只允许重试一次')

  const writeCalls = []
  globalThis.fetch = async (url, init) => {
    writeCalls.push({ url, init })
    return jsonResponse(500, { detail: 'write failed' })
  }
  assertFailure(
    await client.createObject({
      market: 'SZ',
      symbol: '000007',
      名称: '演示新增标的',
      类型: '交易标的',
      资产类型: '股票',
    }),
    'HTTP_ERROR',
    'POST 失败必须转换成统一失败响应',
  )
  assert(writeCalls.length === 1, 'POST 失败绝不能发生第二次请求')
  assert(writeCalls[0]?.init?.method === 'POST', '新增标的必须使用 POST')
  assert(
    JSON.stringify(JSON.parse(writeCalls[0]?.init?.body ?? '{}')) ===
      JSON.stringify({
        market: 'SZ',
        symbol: '000007',
        名称: '演示新增标的',
        类型: '交易标的',
        资产类型: '股票',
      }),
    '新增标的请求体必须严格只含五个契约字段',
  )

  const routeCalls = []
  globalThis.fetch = async (url, init) => {
    routeCalls.push({ url, init })
    return jsonResponse(200, { ok: true, data: {} })
  }
  await client.getStatus()
  await client.getObjects()
  await client.createObject({
    market: 'SZ',
    symbol: '000008',
    名称: '演示路由标的',
    类型: '交易标的',
    资产类型: '股票',
  })
  await client.updateObject('SH/510300', {
    market: 'SH',
    symbol: '510300',
    名称: '演示修改标的',
    类型: '交易标的',
    资产类型: 'ETF',
  })
  await client.deleteObject('SH/510300')
  await client.getAccount()
  await client.getRuns({ limit: 0, from: '2026-08-01', system_name: 'zhixing' })
  await client.getRun('run/001')
  await client.compareRuns({ from: '2026-08-01', to: '2026-08-31' })
  await client.getUsage({ from: '2026-08-01', to: '2026-08-31', group_by: 'day' })
  await client.getPendingInstructions()
  await client.confirmInstruction('instruction/001')
  await client.getSchedule()
  await client.putSchedule({
    时点: ['09:35', '10:00', '11:15', '13:15', '14:00', '14:45'],
    原因: '演示路由校验',
  })
  await client.getCaptcha()
  await client.putCaptcha({
    接口地址: 'https://captcha.demo.invalid/v1',
    模型: '演示验证码模型',
    识别方式: 'vision',
    密钥: '',
    备用识别: [
      {
        接口地址: 'https://ttshitu.demo.invalid/predict',
        模型: '',
        识别方式: 'ttshitu',
        密钥: '',
        uiDraftId: 'local-only',
      },
      {
        接口地址: 'https://chaojiying.demo.invalid/upload',
        模型: '',
        识别方式: 'chaojiying',
        密钥: '',
      },
    ],
  })
  await client.getModelSettings()
  await client.putModelSettings({
    接口地址: 'https://demo.invalid/v1',
    模型: '演示模型-甲',
    提供方: '演示中转',
    协议: 'openai_chat',
    密钥: '',
  })
  await client.getBrokerSettings()
  await client.putBrokerSettings({
    浏览器远端: 'http://browser.example.invalid:4444/wd/hub',
    资金账号: '',
    交易密码: '',
  })
  await client.putUnattended({ 无人值守: true, 原因: '演示路由校验' })

  assert(
    JSON.stringify(routeCalls.map((call) => [call.init?.method, call.url])) ===
      JSON.stringify([
        ['GET', 'https://backend.example.invalid/api/status'],
        ['GET', 'https://backend.example.invalid/api/objects'],
        ['POST', 'https://backend.example.invalid/api/objects'],
        ['PUT', 'https://backend.example.invalid/api/objects/SH%2F510300'],
        ['DELETE', 'https://backend.example.invalid/api/objects/SH%2F510300'],
        ['GET', 'https://backend.example.invalid/api/account'],
        [
          'GET',
          'https://backend.example.invalid/api/runs?limit=0&from=2026-08-01&system_name=zhixing',
        ],
        ['GET', 'https://backend.example.invalid/api/runs/run%2F001'],
        ['GET', 'https://backend.example.invalid/api/runs/compare?from=2026-08-01&to=2026-08-31'],
        ['GET', 'https://backend.example.invalid/api/usage?from=2026-08-01&to=2026-08-31&group_by=day'],
        ['GET', 'https://backend.example.invalid/api/instructions/pending'],
        ['POST', 'https://backend.example.invalid/api/instructions/instruction%2F001/confirm'],
        ['GET', 'https://backend.example.invalid/api/settings/schedule'],
        ['PUT', 'https://backend.example.invalid/api/settings/schedule'],
        ['GET', 'https://backend.example.invalid/api/settings/captcha'],
        ['PUT', 'https://backend.example.invalid/api/settings/captcha'],
        ['GET', 'https://backend.example.invalid/api/settings/model'],
        ['PUT', 'https://backend.example.invalid/api/settings/model'],
        ['GET', 'https://backend.example.invalid/api/settings/broker'],
        ['PUT', 'https://backend.example.invalid/api/settings/broker'],
        ['PUT', 'https://backend.example.invalid/api/settings/unattended'],
      ]),
    'ApiClient 的全部方法必须只映射到契约定义的 HTTP 路径',
  )
  const modelPut = routeCalls.find(
    (call) => call.init?.method === 'PUT' && call.url.endsWith('/api/settings/model'),
  )
  assert(
    JSON.stringify(JSON.parse(modelPut?.init?.body ?? '{}')) ===
      JSON.stringify({
        接口地址: 'https://demo.invalid/v1',
        模型: '演示模型-甲',
        提供方: '演示中转',
        协议: 'openai_chat',
        密钥: '',
      }),
    '模型 PUT 请求体必须不带只读传输字段',
  )
  const captchaPut = routeCalls.find(
    (call) => call.init?.method === 'PUT' && call.url.endsWith('/api/settings/captcha'),
  )
  assert(
    JSON.stringify(JSON.parse(captchaPut?.init?.body ?? '{}')) ===
      JSON.stringify({
        接口地址: 'https://captcha.demo.invalid/v1',
        模型: '演示验证码模型',
        识别方式: 'vision',
        密钥: '',
        备用识别: [
          {
            接口地址: 'https://ttshitu.demo.invalid/predict',
            模型: '',
            识别方式: 'ttshitu',
            密钥: '',
          },
          {
            接口地址: 'https://chaojiying.demo.invalid/upload',
            模型: '',
            识别方式: 'chaojiying',
            密钥: '',
          },
        ],
      }),
    '验证码 PUT 必须严格序列化主服务与备用服务，不得带入界面草稿字段',
  )
  const brokerPut = routeCalls.find(
    (call) => call.init?.method === 'PUT' && call.url.endsWith('/api/settings/broker'),
  )
  assert(
    JSON.stringify(JSON.parse(brokerPut?.init?.body ?? '{}')) ===
      JSON.stringify({
        浏览器远端: 'http://browser.example.invalid:4444/wd/hub',
        资金账号: '',
        交易密码: '',
      }),
    '券商 PUT 请求体必须严格只含三个可写字段',
  )

  console.log('HTTP 客户端校验通过：统一响应、超时、重试边界与契约路径均已覆盖。')
} finally {
  globalThis.fetch = originalFetch
  globalThis.setTimeout = originalSetTimeout
}
