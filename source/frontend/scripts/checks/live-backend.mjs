import { createHttpClient } from '../../src/api/httpClient.ts'

function assert(condition, message) {
  if (!condition) {
    throw new Error(message)
  }
}

function success(response, message) {
  assert(response.ok === true, message)
  return response.data
}

function failure(response, code, message) {
  assert(response.ok === false, message)
  if (!response.ok) {
    assert(response.error.code === code, `${message}：错误码必须是 ${code}`)
  }
  return response
}

const configuredBaseUrl = (
  process.env.ZHIXING_TEST_API_BASE_URL ?? process.env.VITE_API_BASE_URL ?? ''
).trim()

if (!configuredBaseUrl) {
  throw new Error('请设置 ZHIXING_TEST_API_BASE_URL 后再运行真实后端联调。')
}

const baseUrl = configuredBaseUrl.replace(/\/+$/, '')
const expectedOrigin = process.env.ZHIXING_TEST_ORIGIN ?? 'http://127.0.0.1:5173'
const client = createHttpClient(baseUrl)
const draft = {
  market: 'SZ',
  symbol: '987654',
  名称: '联调演示标的',
  类型: '交易标的',
  资产类型: '股票',
}
let created = false

try {
  const preflight = await fetch(`${baseUrl}/api/status`, {
    method: 'OPTIONS',
    headers: { Origin: expectedOrigin },
  })
  assert(preflight.status === 204, 'CORS 预检必须返回 204')
  assert(
    preflight.headers.get('access-control-allow-origin') === expectedOrigin,
    'CORS 必须回显本次联调的精确 Origin',
  )

  const status = success(await client.getStatus(), '状态接口必须可读')
  assert(status.system_name === 'zhixing', '状态接口必须返回 zhixing')

  const objects = success(await client.getObjects(), '标的列表必须可读')
  assert(Array.isArray(objects), '标的列表必须是数组')
  assert(
    objects.every((object) => object.持仓 === null || typeof object.持仓 === 'object'),
    '持仓必须明确区分 null 与对象',
  )

  const accountResponse = await client.getAccount()
  if (accountResponse.ok) {
    assert(typeof accountResponse.data.采集时间 === 'string', '账户快照必须带采集时间')
    assert(typeof accountResponse.data.账户标识 === 'string', '账户快照必须带遮罩账户标识')
    for (const field of ['总资产', '可用资金', '资金余额', '冻结资金', '证券市值']) {
      assert(
        accountResponse.data[field] === null || typeof accountResponse.data[field] === 'number',
        `账户金额字段 ${field} 必须是 number 或 null`,
      )
    }
  } else {
    failure(accountResponse, 'NO_ACCOUNT_SNAPSHOT', '尚未采集时账户接口必须明确返回空态码')
  }

  const runs = success(await client.getRuns({ limit: 5 }), '归档摘要必须可读')
  assert(Array.isArray(runs), '归档摘要必须是数组')
  if (runs[0]) {
    success(await client.getRun(runs[0].strategy_id), '存在的归档详情必须可读')
  } else {
    failure(await client.getRun('integration-missing-run'), 'NOT_FOUND', '缺失归档必须返回 NOT_FOUND')
  }

  const comparison = success(await client.compareRuns(), '对比接口必须可读')
  assert(Array.isArray(comparison.对比项), '对比接口必须包含对比项数组')

  for (const group_by of ['day', 'object', 'model']) {
    const rows = success(await client.getUsage({ group_by }), `用量 ${group_by} 分组必须可读`)
    assert(Array.isArray(rows), `用量 ${group_by} 分组必须返回数组`)
  }

  const pending = success(await client.getPendingInstructions(), '待接管指令接口必须可读')
  assert(Array.isArray(pending), '待接管指令必须是数组')

  const schedule = success(await client.getSchedule(), '调度配置必须可读')
  assert(Array.isArray(schedule.时点) && schedule.时点.length === 6, '调度配置必须包含六个时点')
  const invalidSchedule = await client.putSchedule({
    时点: [...schedule.时点].reverse(),
    原因: '联调验证乱序会被拒绝',
  })
  failure(invalidSchedule, 'INVALID_SCHEDULE', '乱序调度必须被后端拒绝')
  assert(
    !invalidSchedule.ok && (invalidSchedule.error.问题?.length ?? 0) > 0,
    '乱序调度必须返回问题列表',
  )
  success(
    await client.putSchedule({
      时点: [...schedule.时点],
      原因: '联调验证原计划可原样保存',
    }),
    '合法调度必须可保存',
  )

  const captcha = success(await client.getCaptcha(), '验证码配置必须可读')
  assert(
    JSON.stringify(Object.keys(captcha).sort()) ===
      JSON.stringify(['接口地址', '模型', '识别方式', '密钥', '备用识别'].sort()),
    '验证码配置必须只返回五个契约字段',
  )
  assert(['vision', 'ttshitu', 'chaojiying'].includes(captcha.识别方式), '主识别方式必须合法')
  assert(typeof captcha.密钥 === 'string', '验证码主配置必须只返回脱敏密钥字段')
  assert(Array.isArray(captcha.备用识别), '备用识别必须返回数组')
  for (const backup of captcha.备用识别) {
    assert(
      JSON.stringify(Object.keys(backup).sort()) ===
        JSON.stringify(['接口地址', '模型', '识别方式', '密钥'].sort()),
      '备用识别必须只返回四个契约字段',
    )
    assert(['vision', 'ttshitu', 'chaojiying'].includes(backup.识别方式), '备用识别方式必须合法')
    assert(typeof backup.密钥 === 'string', '备用识别必须只返回脱敏密钥字段')
  }
  success(
    await client.putCaptcha({
      接口地址: captcha.接口地址,
      模型: captcha.模型,
      识别方式: captcha.识别方式,
      密钥: '',
      备用识别: captcha.备用识别.map((backup) => ({
        接口地址: backup.接口地址,
        模型: backup.模型,
        识别方式: backup.识别方式,
        密钥: '',
      })),
    }),
    '原样回放非密钥字段时，空密钥必须逐项保留现值',
  )

  const broker = success(await client.getBrokerSettings(), '券商配置必须可读')
  assert(
    JSON.stringify(Object.keys(broker).sort()) ===
      JSON.stringify(['浏览器远端', '资金账号', '交易密码已配置', '缺项', '已配全'].sort()),
    '券商配置 GET 必须只返回五个只读字段',
  )
  assert(!Object.hasOwn(broker, '交易密码'), '券商配置 GET 不得返回交易密码字段')

  success(
    await client.putUnattended({
      无人值守: false,
      原因: '隔离联调环境保持关闭',
    }),
    '隔离环境必须能明确关闭无人值守',
  )

  success(await client.createObject(draft), '隔离环境必须能新增演示标的')
  created = true
  success(
    await client.updateObject(`SZ_${draft.symbol}`, {
      ...draft,
      名称: '联调演示标的已修改',
    }),
    '隔离环境必须能修改演示标的',
  )
  failure(
    await client.updateObject(`SZ_${draft.symbol}`, { ...draft, market: 'SH' }),
    'IDENTITY_IMMUTABLE',
    '修改标的身份必须被拒绝',
  )
  success(await client.deleteObject(`SZ_${draft.symbol}`), '隔离环境必须能删除演示标的')
  created = false
  failure(
    await client.deleteObject(`SZ_${draft.symbol}`),
    'NOT_FOUND',
    '重复删除必须返回正常的记录不存在结果',
  )

  const missingRoute = await fetch(`${baseUrl}/api/integration-missing-route`)
  const missingPayload = await missingRoute.json()
  assert(
    missingRoute.status === 404 && missingPayload?.error?.code === 'NO_SUCH_ENDPOINT',
    '不存在的接口必须与记录级 NOT_FOUND 分开',
  )

  console.log('真实后端联调通过：账户快照、券商只读配置与安全路由均已验证；确认下单路由未调用。')
} finally {
  if (created) {
    await client.deleteObject(`SZ_${draft.symbol}`)
  }
}
