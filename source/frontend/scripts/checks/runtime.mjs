import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import statusFixture from '../../src/fixtures/status/dry-run.json' with { type: 'json' }
import neverSucceededStatusFixture from '../../src/fixtures/status/never-succeeded.json' with { type: 'json' }
import stalledStatusFixture from '../../src/fixtures/status/stalled.json' with { type: 'json' }
import {
  createRuntimeFixtureApi,
  runtimeEndpoints,
} from '../../src/api/types.runtime.ts'

const frontendRoot = fileURLToPath(new URL('../../', import.meta.url))

function assert(condition, message) {
  if (!condition) {
    throw new Error(message)
  }
}

async function readJson(relativePath) {
  const content = await readFile(path.join(frontendRoot, relativePath), 'utf8')
  return JSON.parse(content)
}

function assertSuccess(response, message) {
  assert(response.ok === true, message)
  return response.data
}

function assertFailure(response, message) {
  assert(response.ok === false, message)
  assert(Boolean(response.error?.code), `${message}：缺少错误 code`)
  assert(Boolean(response.error?.message), `${message}：缺少面向人的错误 message`)
}

function assertUsageRows(rows, message) {
  for (const row of rows) {
    for (const field of [
      '轮数',
      'input_tokens',
      'output_tokens',
      'reasoning_tokens',
      'cached_tokens',
    ]) {
      assert(Number.isInteger(row[field]) && row[field] >= 0, `${message}：${field} 必须是非负整数`)
    }
    assert(
      typeof row.缓存命中率 === 'number' && row.缓存命中率 >= 0 && row.缓存命中率 <= 1,
      `${message}：缓存命中率必须处于 0 到 1 之间`,
    )
  }
}

function sourceSection(source, startMarker, endMarker, label) {
  const start = source.indexOf(startMarker)
  const end = source.indexOf(endMarker, start + startMarker.length)
  assert(start >= 0 && end > start, `${label}源码区段不存在或边界不完整`)
  return source.slice(start, end)
}

const [
  scheduleFixture,
  captchaFixture,
  brokerSettingsFixture,
  modelSettingsFixture,
  writeOkFixture,
  normalUsageFixture,
  lowUsageFixture,
  emptyUsageFixture,
  runtimePageSource,
  usageSource,
  runtimeCss,
] = await Promise.all([
  readJson('src/fixtures/settings/schedule.json'),
  readJson('src/fixtures/settings/captcha.json'),
  readJson('src/fixtures/settings/broker.json'),
  readJson('src/fixtures/settings/model.json'),
  readJson('src/fixtures/settings/write-ok.json'),
  readJson('src/fixtures/usage/day-normal.json'),
  readJson('src/fixtures/usage/day-low.json'),
  readJson('src/fixtures/usage/empty.json'),
  readFile(path.join(frontendRoot, 'src/pages/RuntimePage.tsx'), 'utf8'),
  readFile(path.join(frontendRoot, 'src/components/UsageOverview.tsx'), 'utf8'),
  readFile(path.join(frontendRoot, 'src/styles/runtime.css'), 'utf8'),
])

// 这里只锁 fixture 视图模型；后端 JSON 字段仍须由契约另行冻结。
const schedule = assertSuccess(scheduleFixture, '调度 fixture 必须是成功响应')
assert(Array.isArray(schedule.时点) && schedule.时点.length === 6, '调度 fixture 必须恰好有六个时点')
assert(schedule.时点.every((time) => /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(time)), '调度时点必须是合法 HH:mm')
assert(new Set(schedule.时点).size === 6, '六个调度时点不得重复')
assert(JSON.stringify(schedule.时点) === JSON.stringify([...schedule.时点].sort()), '调度时点必须按时间升序')

const captcha = assertSuccess(captchaFixture, '验证码 fixture 必须是成功响应')
assert(
  JSON.stringify(Object.keys(captcha).sort()) ===
    JSON.stringify(['接口地址', '模型', '识别方式', '密钥', '备用识别'].sort()),
  '验证码主配置必须恰好返回五个契约字段',
)
assert(['vision', 'ttshitu', 'chaojiying'].includes(captcha.识别方式), '主识别方式必须是契约允许值')
assert(captcha.密钥.includes('****'), '验证码主密钥必须始终是脱敏值')
assert(Array.isArray(captcha.备用识别), '备用识别必须始终是数组')
for (const backup of captcha.备用识别) {
  assert(
    JSON.stringify(Object.keys(backup).sort()) ===
      JSON.stringify(['接口地址', '模型', '识别方式', '密钥'].sort()),
    '每条备用识别必须恰好返回四个契约字段',
  )
  assert(['vision', 'ttshitu', 'chaojiying'].includes(backup.识别方式), '备用识别方式必须是契约允许值')
  assert(backup.密钥.includes('****'), '备用识别密钥必须始终是脱敏值')
}
assert(!JSON.stringify(captchaFixture).includes('sk-plain'), '验证码 fixture 不得包含明文密钥')
const brokerSettings = assertSuccess(brokerSettingsFixture, '券商配置 fixture 必须是成功响应')
assert(
  JSON.stringify(Object.keys(brokerSettings).sort()) ===
    JSON.stringify(['浏览器远端', '资金账号', '交易密码已配置', '缺项', '已配全'].sort()),
  '券商配置 GET 必须恰好返回五个契约字段',
)
assert(brokerSettings.资金账号.includes('****'), '券商账号必须始终是遮罩值')
assert(!Object.hasOwn(brokerSettings, '交易密码'), '券商配置 GET 不得包含交易密码字段')
const modelSettings = assertSuccess(modelSettingsFixture, '模型配置 fixture 必须是成功响应')
assert(
  JSON.stringify(Object.keys(modelSettings).sort()) ===
    JSON.stringify(['传输', '协议', '提供方', '密钥', '模型', '接口地址'].sort()),
  '模型配置 GET 必须恰好返回六个契约字段',
)
assert(modelSettings.接口地址 === 'https://demo.invalid/v1', '模型 fixture 必须使用编造的接口地址')
assert(modelSettings.模型 === '演示模型-甲', '模型 fixture 必须使用编造的模型名')
assert(modelSettings.提供方 === '演示中转', '模型 fixture 必须使用编造的提供方')
assert(modelSettings.密钥 === '****1234', '模型 fixture 只允许固定脱敏密钥')
assert(
  ['openai_chat', 'anthropic_messages'].includes(modelSettings.协议),
  '模型 fixture 协议必须是契约允许值',
)
assert(
  ['https', 'http(本机回环,不出网卡)', '明文 http(密钥与整份上下文会摊在链路上)', '未知'].includes(modelSettings.传输),
  '模型 fixture 传输必须是契约四个只读取值之一',
)
assertSuccess(writeOkFixture, '写配置 fixture 必须返回 ok:true')

const normalUsage = assertSuccess(normalUsageFixture, '正常用量 fixture 必须是成功响应')
const lowUsage = assertSuccess(lowUsageFixture, '低命中用量 fixture 必须是成功响应')
const emptyUsage = assertSuccess(emptyUsageFixture, '零数据用量 fixture 必须是成功响应')
assertUsageRows(normalUsage, '正常用量 fixture')
assertUsageRows(lowUsage, '低命中用量 fixture')
assert(normalUsage.length > 0 && normalUsage.every((row) => row.缓存命中率 >= 0.5), '正常用量 fixture 必须覆盖命中率正常场景')
assert(lowUsage.some((row) => row.缓存命中率 < 0.1), '低命中 fixture 必须至少包含一条低于 10% 的记录')
assert(Array.isArray(emptyUsage) && emptyUsage.length === 0, '零数据 fixture 必须是 ok:true 的空数组')

const runtimeApi = createRuntimeFixtureApi(() => 'default')
const initialSchedule = assertSuccess(await runtimeApi.getSchedule(), 'fixture API 必须能读取调度')
const nextTimes = ['09:40', '10:05', '11:20', '13:20', '14:05', '14:50']
assertSuccess(
  await runtimeApi.putSchedule({ 时点: nextTimes, 原因: 'fixture 调整调度时点' }),
  '调度 PUT 必须成功',
)
assert(
  JSON.stringify(assertSuccess(await runtimeApi.getSchedule(), '调度 PUT 后必须可读取').时点) === JSON.stringify(nextTimes),
  '调度 PUT 成功后必须反映六个新时点',
)
assertFailure(
  await runtimeApi.putSchedule({ 时点: ['09:35', '09:35'], 原因: '' }),
  '非法调度 PUT 不得静默成功',
)
assert(JSON.stringify(initialSchedule.时点) !== JSON.stringify(nextTimes), '调度写入测试必须真的改变状态')

const beforeCaptcha = assertSuccess(await runtimeApi.getCaptcha(), 'fixture API 必须能读取验证码配置')
const blankBackupSecrets = beforeCaptcha.备用识别.map((recognizer) => ({
  接口地址: recognizer.接口地址,
  模型: recognizer.模型,
  识别方式: recognizer.识别方式,
  密钥: '',
}))
assertSuccess(
  await runtimeApi.putCaptcha({
    接口地址: 'https://captcha.example.invalid/v2',
    模型: 'demo-captcha-next',
    识别方式: 'vision',
    密钥: '',
    备用识别: blankBackupSecrets,
  }),
  '主密钥和原顺序备用密钥留空时验证码 PUT 必须成功',
)
const afterBlankSecret = assertSuccess(await runtimeApi.getCaptcha(), '空密钥 PUT 后配置必须可读取')
assert(afterBlankSecret.密钥 === beforeCaptcha.密钥, '密钥留空必须保留原脱敏值')
assert(
  JSON.stringify(afterBlankSecret.备用识别.map((recognizer) => recognizer.密钥)) ===
    JSON.stringify(beforeCaptcha.备用识别.map((recognizer) => recognizer.密钥)),
  '备用项保持原顺序时，空密钥必须逐项保留原脱敏值',
)
const submittedPlaceholder = 'fixture-secret-placeholder-zz99'
const submittedBackupPlaceholder = 'fixture-backup-placeholder-yy88'
assertSuccess(
  await runtimeApi.putCaptcha({
    接口地址: afterBlankSecret.接口地址,
    模型: afterBlankSecret.模型,
    识别方式: afterBlankSecret.识别方式,
    密钥: submittedPlaceholder,
    备用识别: afterBlankSecret.备用识别.map((recognizer, index) => ({
      接口地址: recognizer.接口地址,
      模型: recognizer.模型,
      识别方式: recognizer.识别方式,
      密钥: index === 0 ? submittedBackupPlaceholder : '',
    })),
  }),
  '填写主密钥或备用密钥时验证码 PUT 必须成功',
)
const afterSecretChange = assertSuccess(await runtimeApi.getCaptcha(), '覆盖密钥后配置必须可读取')
assert(afterSecretChange.密钥 === 'sk-****zz99', '覆盖后只允许返回脱敏密钥')
assert(afterSecretChange.备用识别[0].密钥 === 'sk-****yy88', '备用密钥覆盖后只允许返回脱敏值')
assert(!JSON.stringify(afterSecretChange).includes(submittedPlaceholder), 'GET 不得回显刚提交的明文密钥')
assert(!JSON.stringify(afterSecretChange).includes(submittedBackupPlaceholder), 'GET 不得回显刚提交的备用明文密钥')

const beforeInvalidBackup = structuredClone(afterSecretChange)
assertFailure(
  await runtimeApi.putCaptcha({
    接口地址: afterSecretChange.接口地址,
    模型: afterSecretChange.模型,
    识别方式: afterSecretChange.识别方式,
    密钥: '',
    备用识别: [
      ...afterSecretChange.备用识别.map((recognizer) => ({ ...recognizer, 密钥: '' })),
      {
        接口地址: 'https://new-backup.example.invalid/v1',
        模型: 'new-backup-model',
        识别方式: 'vision',
        密钥: '',
      },
    ],
  }),
  '新增备用项不填写密钥必须拒绝',
)
assert(
  JSON.stringify(assertSuccess(await runtimeApi.getCaptcha(), '新增备用失败后配置必须可读')) ===
    JSON.stringify(beforeInvalidBackup),
  '新增备用失败不得污染原配置',
)

assertFailure(
  await runtimeApi.putCaptcha({
    接口地址: afterSecretChange.接口地址,
    模型: afterSecretChange.模型,
    识别方式: afterSecretChange.识别方式,
    密钥: '',
    备用识别: [...afterSecretChange.备用识别].reverse().map((recognizer) => ({
      ...recognizer,
      密钥: '',
    })),
  }),
  '调换已有备用项时不得错位沿用空密钥',
)

assertSuccess(
  await runtimeApi.putCaptcha({
    接口地址: afterSecretChange.接口地址,
    模型: afterSecretChange.模型,
    识别方式: afterSecretChange.识别方式,
    密钥: '',
    备用识别: [],
  }),
  '空备用数组必须按当前后端契约表示不修改',
)
assert(
  assertSuccess(await runtimeApi.getCaptcha(), '空备用数组写入后配置必须可读').备用识别.length ===
    afterSecretChange.备用识别.length,
  '空备用数组不得误清空已有备用服务',
)

assertFailure(
  await runtimeApi.putCaptcha({
    接口地址: afterSecretChange.接口地址,
    模型: afterSecretChange.模型,
    识别方式: 'unknown-provider',
    密钥: '',
    备用识别: blankBackupSecrets,
  }),
  '未知识别方式必须拒绝',
)

const beforeBroker = assertSuccess(await runtimeApi.getBrokerSettings(), 'fixture API 必须能读取券商配置')
assertSuccess(
  await runtimeApi.putBrokerSettings({
    浏览器远端: beforeBroker.浏览器远端,
    资金账号: '',
    交易密码: '',
  }),
  '账号和密码留空时券商 PUT 必须成功',
)
assert(
  JSON.stringify(assertSuccess(await runtimeApi.getBrokerSettings(), '空机密 PUT 后券商配置必须可读取')) ===
    JSON.stringify(beforeBroker),
  '账号和密码留空必须保留原遮罩值与配置状态',
)
const submittedBrokerAccount = ['fixture', 'account', 'zz99'].join('-')
const submittedBrokerPassword = ['fixture', 'password', 'zz88'].join('-')
assertSuccess(
  await runtimeApi.putBrokerSettings({
    浏览器远端: beforeBroker.浏览器远端,
    资金账号: submittedBrokerAccount,
    交易密码: submittedBrokerPassword,
  }),
  '填写新账号和密码时券商 PUT 必须成功',
)
const afterBrokerSecrets = assertSuccess(
  await runtimeApi.getBrokerSettings(),
  '覆盖券商机密后配置必须可读取',
)
assert(afterBrokerSecrets.资金账号.includes('****'), '覆盖账号后 GET 只允许返回遮罩值')
assert(afterBrokerSecrets.交易密码已配置 === true, '覆盖密码后 GET 只允许返回已配置状态')
assert(!Object.hasOwn(afterBrokerSecrets, '交易密码'), '覆盖密码后 GET 仍不得出现交易密码字段')
assert(
  !JSON.stringify(afterBrokerSecrets).includes(submittedBrokerAccount) &&
    !JSON.stringify(afterBrokerSecrets).includes(submittedBrokerPassword),
  '券商 GET 不得回显刚提交的账号或密码明文',
)
assertFailure(
  await runtimeApi.putBrokerSettings({ 浏览器远端: ' ', 资金账号: '', 交易密码: '' }),
  '空浏览器远端必须返回 INVALID_BROKER_SETTINGS',
)
const malformedBroker = await runtimeApi.putBrokerSettings({})
assertFailure(malformedBroker, '缺少券商配置字段不得抛出异常')
assert(
  malformedBroker.error.code === 'INVALID_BROKER_SETTINGS' &&
    (malformedBroker.error.问题?.length ?? 0) === 3,
  '缺少券商配置字段必须一次返回全部问题',
)

const beforeModel = assertSuccess(await runtimeApi.getModelSettings(), 'fixture API 必须能读取模型配置')
assertSuccess(
  await runtimeApi.putModelSettings({
    接口地址: 'http://relay.example.invalid/v2',
    模型: '演示模型-乙',
    提供方: '演示中转-乙',
    协议: 'anthropic_messages',
    密钥: '',
  }),
  '明文 http 的模型 PUT 必须成功并靠传输字段持续提示',
)
const afterBlankModelSecret = assertSuccess(
  await runtimeApi.getModelSettings(),
  '模型 PUT 后配置必须可读取',
)
assert(afterBlankModelSecret.密钥 === beforeModel.密钥, '模型密钥留空必须保留原脱敏值')
assert(
  afterBlankModelSecret.传输 === '明文 http(密钥与整份上下文会摊在链路上)',
  '非回环 http 必须由 fixture 标记为明文传输',
)
const submittedModelValue = ['fixture', 'model', 'value', 'zz99'].join('-')
assertSuccess(
  await runtimeApi.putModelSettings({
    接口地址: afterBlankModelSecret.接口地址,
    模型: afterBlankModelSecret.模型,
    提供方: afterBlankModelSecret.提供方,
    协议: afterBlankModelSecret.协议,
    密钥: submittedModelValue,
  }),
  '填写模型密钥时 PUT 必须成功',
)
const afterModelSecretChange = assertSuccess(
  await runtimeApi.getModelSettings(),
  '覆盖模型密钥后配置必须可读取',
)
assert(afterModelSecretChange.密钥 === '****zz99', '模型密钥覆盖后只允许返回脱敏值')
assert(
  !JSON.stringify(afterModelSecretChange).includes(submittedModelValue),
  '模型 GET 不得回显刚提交的明文密钥',
)
const invalidModel = await runtimeApi.putModelSettings({
  接口地址: 'relay.example.invalid',
  模型: ' ',
  提供方: '',
  协议: 'unknown_protocol',
  密钥: '',
})
assertFailure(invalidModel, '模型配置必须一次返回全部前端可知问题')
assert(
  invalidModel.error.问题?.some((problem) => problem.code === 'ENDPOINT_SCHEME') &&
    invalidModel.error.问题?.some((problem) => problem.code === 'MODEL_REQUIRED') &&
    invalidModel.error.问题?.some((problem) => problem.code === 'PROVIDER_REQUIRED') &&
    invalidModel.error.问题?.some((problem) => problem.code === 'UNKNOWN_PROTOCOL'),
  '模型配置 fixture 必须一次收齐协议、地址、模型与提供方问题',
)
assert(
  JSON.stringify(assertSuccess(await runtimeApi.getModelSettings(), '非法模型写入后必须可读取')) ===
    JSON.stringify(afterModelSecretChange),
  '模型配置校验失败不得污染原状态',
)

const modelTransportCases = [
  ['https://relay.example.invalid/v3', 'https'],
  ['http://localhost:8080/v1', 'http(本机回环,不出网卡)'],
  ['http://127.0.0.1:8080/v1', 'http(本机回环,不出网卡)'],
  ['http://[::1]:8080/v1', 'http(本机回环,不出网卡)'],
  ['http://', '未知'],
]
for (const [endpoint, expectedTransport] of modelTransportCases) {
  assertSuccess(
    await runtimeApi.putModelSettings({
      接口地址: endpoint,
      模型: '演示传输检测模型',
      提供方: '演示传输检测中转',
      协议: 'openai_chat',
      密钥: '',
    }),
    `传输 ${expectedTransport} 的模型 PUT 必须成功`,
  )
  assert(
    assertSuccess(await runtimeApi.getModelSettings(), `传输 ${expectedTransport} 必须可读取`).传输 === expectedTransport,
    `接口地址 ${endpoint} 必须映射为传输 ${expectedTransport}`,
  )
}

const firstConfigurationApi = createRuntimeFixtureApi(
  () => 'default',
  { initialModelSecretConfigured: false },
)
const firstConfigurationBefore = assertSuccess(
  await firstConfigurationApi.getModelSettings(),
  '首次配置 fixture 仍必须可读取当前设置快照',
)
const firstConfigurationFailure = await firstConfigurationApi.putModelSettings({
  接口地址: 'https://first-setup.example.invalid/v1',
  模型: '首次配置演示模型',
  提供方: '首次配置演示中转',
  协议: 'openai_chat',
  密钥: '',
})
assertFailure(firstConfigurationFailure, '首次配置空密钥必须返回 ok:false')
assert(
  firstConfigurationFailure.error.code === 'INVALID_MODEL_SETTINGS' &&
    firstConfigurationFailure.error.问题?.some((problem) => problem.code === 'SECRET_REQUIRED'),
  '首次配置空密钥必须返回 INVALID_MODEL_SETTINGS 与 SECRET_REQUIRED',
)
assert(
  JSON.stringify(assertSuccess(await firstConfigurationApi.getModelSettings(), '首次配置失败后必须可读取')) ===
    JSON.stringify(firstConfigurationBefore),
  '首次配置空密钥失败不得污染现有设置快照',
)
const malformedModelInput = await firstConfigurationApi.putModelSettings({})
assertFailure(malformedModelInput, '缺少模型字段不得抛出异常')
assert(
  malformedModelInput.error.code === 'INVALID_MODEL_SETTINGS' &&
    malformedModelInput.error.问题?.some((problem) => problem.code === 'ENDPOINT_SCHEME') &&
    malformedModelInput.error.问题?.some((problem) => problem.code === 'MODEL_REQUIRED') &&
    malformedModelInput.error.问题?.some((problem) => problem.code === 'PROVIDER_REQUIRED') &&
    malformedModelInput.error.问题?.some((problem) => problem.code === 'SECRET_REQUIRED'),
  '缺少模型字段必须一次返回 INVALID_MODEL_SETTINGS 的全部问题',
)

const unattendedBefore = statusFixture.data.无人值守
assertFailure(
  await runtimeApi.putUnattended({ 无人值守: !unattendedBefore, 原因: '   ' }),
  '空白原因必须拒绝无人值守 PUT',
)
assert(statusFixture.data.无人值守 === unattendedBefore, '空白原因不得改变无人值守状态')
assertSuccess(
  await runtimeApi.putUnattended({ 无人值守: !unattendedBefore, 原因: 'fixture 审计原因' }),
  '带原因的无人值守 PUT 必须成功',
)
assert(statusFixture.data.无人值守 === !unattendedBefore, '带原因的无人值守 PUT 必须改变顶栏数据源')
statusFixture.data.无人值守 = unattendedBefore

const writeFailureApi = createRuntimeFixtureApi(() => 'runtime-write-error')
const scheduleBeforeFailure = assertSuccess(await writeFailureApi.getSchedule(), '写失败场景仍须能读取调度')
const captchaBeforeFailure = assertSuccess(await writeFailureApi.getCaptcha(), '写失败场景仍须能读取验证码配置')
const modelBeforeFailure = assertSuccess(await writeFailureApi.getModelSettings(), '写失败场景仍须能读取模型配置')
const brokerBeforeFailure = assertSuccess(await writeFailureApi.getBrokerSettings(), '写失败场景仍须能读取券商配置')
assertFailure(
  await writeFailureApi.putSchedule({ 时点: nextTimes, 原因: 'fixture 写失败调度' }),
  '调度写失败必须返回 ok:false',
)
assertFailure(
  await writeFailureApi.putCaptcha({
    接口地址: 'https://captcha.example.invalid/v3',
    模型: 'changed',
    识别方式: 'vision',
    密钥: '',
    备用识别: captchaBeforeFailure.备用识别.map((recognizer) => ({
      ...recognizer,
      密钥: '',
    })),
  }),
  '验证码写失败必须返回 ok:false',
)
assertFailure(
  await writeFailureApi.putUnattended({ 无人值守: !unattendedBefore, 原因: 'fixture 写失败' }),
  '无人值守写失败必须返回 ok:false',
)
assertFailure(
  await writeFailureApi.putModelSettings({
    接口地址: 'https://demo.invalid/v2',
    模型: '演示模型-丙',
    提供方: '演示中转-丙',
    协议: 'openai_chat',
    密钥: '',
  }),
  '模型写失败必须返回 ok:false',
)
assertFailure(
  await writeFailureApi.putBrokerSettings({
    浏览器远端: brokerBeforeFailure.浏览器远端,
    资金账号: '',
    交易密码: '',
  }),
  '券商配置写失败必须返回 ok:false',
)
assert(
  JSON.stringify(assertSuccess(await writeFailureApi.getSchedule(), '写失败后调度必须可读取')) === JSON.stringify(scheduleBeforeFailure),
  '调度写失败不得污染原状态',
)
assert(
  JSON.stringify(assertSuccess(await writeFailureApi.getCaptcha(), '写失败后验证码配置必须可读取')) === JSON.stringify(captchaBeforeFailure),
  '验证码写失败不得污染原状态',
)
assert(
  JSON.stringify(assertSuccess(await writeFailureApi.getModelSettings(), '写失败后模型配置必须可读取')) === JSON.stringify(modelBeforeFailure),
  '模型写失败不得污染原状态',
)
assert(
  JSON.stringify(assertSuccess(await writeFailureApi.getBrokerSettings(), '写失败后券商配置必须可读取')) === JSON.stringify(brokerBeforeFailure),
  '券商配置写失败不得污染原状态',
)

const defaultDayUsage = assertSuccess(
  await runtimeApi.getUsage({ group_by: 'day' }),
  '按日用量必须可读取',
)
assert(defaultDayUsage.every((row) => '日期' in row), '按日分组必须返回日期标签')
const filteredDayUsage = assertSuccess(
  await runtimeApi.getUsage({ from: '2099-01-01', to: '2099-01-31', group_by: 'day' }),
  '区间过滤必须返回统一响应',
)
assert(filteredDayUsage.length === 0, '区间内无数据必须返回成功空数组')
const objectUsage = assertSuccess(
  await runtimeApi.getUsage({ group_by: 'object' }),
  '按标的用量必须可读取',
)
assert(objectUsage.length > 0 && objectUsage.every((row) => 'object_id' in row), '按标的分组必须有标的标签')
const modelUsage = assertSuccess(
  await runtimeApi.getUsage({ group_by: 'model' }),
  '按模型用量必须可读取',
)
assert(modelUsage.length > 0 && modelUsage.every((row) => 'model' in row), '按模型分组必须有模型标签')
assert(
  assertSuccess(
    await runtimeApi.getUsage({ from: '2099-01-01', to: '2099-01-31', group_by: 'object' }),
    '按标的的区间外查询必须返回统一响应',
  ).length === 0,
  '按标的聚合不得返回区间外数据',
)
assert(
  assertSuccess(
    await runtimeApi.getUsage({ from: '2099-01-01', to: '2099-01-31', group_by: 'model' }),
    '按模型的区间外查询必须返回统一响应',
  ).length === 0,
  '按模型聚合不得返回区间外数据',
)
assertFailure(
  await runtimeApi.getUsage({ from: '2026-08-18', to: '2026-08-17', group_by: 'day' }),
  '倒置日期区间必须返回 ok:false',
)
assertFailure(
  await runtimeApi.getUsage({ group_by: 'week' }),
  '契约外的用量分组必须返回 ok:false',
)
const lowApi = createRuntimeFixtureApi(() => 'usage-low')
const lowRows = assertSuccess(await lowApi.getUsage({ group_by: 'day' }), '低命中场景必须可读取')
assert(lowRows.some((row) => row.缓存命中率 < 0.1), '低命中 API 场景必须保留低于 10% 的记录')
const emptyApi = createRuntimeFixtureApi(() => 'usage-empty')
assert(assertSuccess(await emptyApi.getUsage({ group_by: 'day' }), '零用量场景必须可读取').length === 0, '零用量 API 场景必须返回空数组')
const readFailureApi = createRuntimeFixtureApi(() => 'runtime-error')
assertFailure(await readFailureApi.getSchedule(), '调度 GET 错误态必须可达')
assertFailure(await readFailureApi.getCaptcha(), '验证码 GET 错误态必须可达')
assertFailure(await readFailureApi.getModelSettings(), '模型 GET 错误态必须可达')
assertFailure(await readFailureApi.getBrokerSettings(), '券商 GET 错误态必须可达')
assertFailure(await readFailureApi.getUsage({ group_by: 'day' }), '用量 GET 错误态必须可达')

const stalledBefore = stalledStatusFixture.data.无人值守
const stalledApi = createRuntimeFixtureApi(() => 'stalled')
assertSuccess(
  await stalledApi.putUnattended({ 无人值守: !stalledBefore, 原因: '停摆场景写入验收' }),
  '停摆场景的无人值守 PUT 必须成功',
)
assert(stalledStatusFixture.data.无人值守 === !stalledBefore, '停摆场景必须更新对应顶栏数据源')
stalledStatusFixture.data.无人值守 = stalledBefore

const neverSucceededBefore = neverSucceededStatusFixture.data.无人值守
const neverSucceededApi = createRuntimeFixtureApi(() => 'never-success')
assertSuccess(
  await neverSucceededApi.putUnattended({ 无人值守: !neverSucceededBefore, 原因: '首次部署场景写入验收' }),
  '首次部署场景的无人值守 PUT 必须成功',
)
assert(
  neverSucceededStatusFixture.data.无人值守 === !neverSucceededBefore,
  '首次部署场景必须更新对应顶栏数据源',
)
neverSucceededStatusFixture.data.无人值守 = neverSucceededBefore

assert(
  JSON.stringify(runtimeEndpoints) === JSON.stringify({
    schedule: '/api/settings/schedule',
    captcha: '/api/settings/captcha',
    model: '/api/settings/model',
    broker: '/api/settings/broker',
    unattended: '/api/settings/unattended',
    usage: '/api/usage',
  }),
  '运行页 API 路径必须与契约一致',
)

const componentSource = `${runtimePageSource}\n${usageSource}`
const modelSectionSource = sourceSection(
  runtimePageSource,
  'function ModelSettingsSection()',
  'function BrokerSettingsSection()',
  '模型配置',
)
const captchaSectionSource = sourceSection(
  runtimePageSource,
  'interface CaptchaRecognizerIdentity',
  'interface ModelSettingsProblem',
  '验证码配置',
)
const brokerSectionSource = sourceSection(
  runtimePageSource,
  'function BrokerSettingsSection()',
  'export function RuntimePage()',
  '券商配置',
)
assert(!/\bfetch\s*\(/.test(componentSource), '运行页组件不得直接调用 fetch')
assert(!/from\s+['"][^'"]*fixtures\//.test(componentSource), '运行页组件不得直接导入 fixture')
for (const forbiddenControl of ['一键全自动', '刷新数据', '采集账户', '显示明文']) {
  assert(!componentSource.includes(forbiddenControl), `运行页不得出现 ${forbiddenControl} 控件`)
}
assert(runtimePageSource.includes('reason.trim()'), '无人值守原因必须 trim 后校验')
assert(runtimePageSource.includes('required'), '无人值守原因输入必须保留浏览器必填约束')
assert(runtimePageSource.includes('disabled={!canSubmit}'), '原因未填或目标无效时提交按钮必须禁用')
for (const method of ['vision', 'ttshitu', 'chaojiying']) {
  assert(captchaSectionSource.includes(`value: '${method}'`), `验证码配置必须提供 ${method} 识别方式`)
}
assert(captchaSectionSource.includes('备用识别: backups.map'), '验证码 PUT 必须显式提交备用识别数组')
assert(captchaSectionSource.includes('+ 添加备用服务'), '验证码配置必须支持追加备用服务')
assert(captchaSectionSource.includes('移除未保存项'), '未提交的备用服务必须可以撤销')
assert(captchaSectionSource.includes('已保存项目必须保持顺序'), '界面必须说明已保存备用项的顺序约束')
assert(captchaSectionSource.includes('value={newSecret}'), '主识别密码框必须只绑定新的输入值')
assert(captchaSectionSource.includes('value={draft.newSecret}'), '备用识别密码框必须只绑定新的输入值')
assert(!captchaSectionSource.includes('value={currentSecret}'), '主识别脱敏密钥不得回填到输入框')
assert(!captchaSectionSource.includes('value={draft.currentSecret}'), '备用脱敏密钥不得回填到输入框')
assert(captchaSectionSource.includes("setNewSecret('')"), '读取或保存后主识别密码框必须清空')
assert(captchaSectionSource.includes("newSecret: ''"), '读取或保存后备用密码框必须清空')
assert(modelSectionSource.includes('type="password"'), '模型新密钥必须使用 password 输入')
assert(modelSectionSource.includes('placeholder="留空表示不修改"'), '模型密钥输入必须明确留空不修改')
assert(modelSectionSource.includes('value={newSecret}'), '模型密码框必须只绑定新的输入值')
assert(modelSectionSource.includes("setNewSecret('')"), '读取或保存后模型密码框必须清空')
assert(!modelSectionSource.includes('value={modelSettings.data.密钥}'), '模型密码框不得回填脱敏密钥')
assert(!modelSectionSource.includes('setNewSecret(modelSettings.data.密钥)'), '模型状态不得把脱敏密钥写入密码框')
assert(modelSectionSource.includes('getModelSettings'), '运行页必须读取模型配置')
assert(modelSectionSource.includes('putModelSettings'), '运行页必须写入模型配置')
assert(modelSectionSource.includes('<select'), '模型协议必须使用下拉框')
assert(modelSectionSource.includes('openai_chat'), '模型协议必须提供 openai_chat')
assert(modelSectionSource.includes('anthropic_messages'), '模型协议必须提供 anthropic_messages')
assert(modelSectionSource.includes('model-transport'), '运行页必须常驻展示只读传输字段')
assert(!modelSectionSource.includes('stream:false'), '模型配置不得出现流式开关')
assert(brokerSectionSource.includes('getBrokerSettings'), '运行页必须读取券商配置')
assert(brokerSectionSource.includes('putBrokerSettings'), '运行页必须写入券商配置')
assert((brokerSectionSource.match(/type="password"/g) ?? []).length === 2, '资金账号与交易密码都必须使用 password 输入')
assert((brokerSectionSource.match(/placeholder="留空表示不修改"/g) ?? []).length === 2, '两个券商机密输入都必须明确留空不修改')
assert(brokerSectionSource.includes('value={newAccount}'), '券商账号输入必须只绑定新的输入值')
assert(brokerSectionSource.includes('value={newPassword}'), '交易密码输入必须只绑定新的输入值')
assert(brokerSectionSource.includes("setNewAccount('')"), '读取或保存后券商账号输入必须清空')
assert(brokerSectionSource.includes("setNewPassword('')"), '读取或保存后交易密码输入必须清空')
assert(!brokerSectionSource.includes('value={brokerSettings.data.资金账号}'), '遮罩账号不得回填到可提交输入')
assert(!brokerSectionSource.includes('setNewAccount(brokerSettings.data.资金账号)'), '券商状态不得把遮罩账号写入新账号输入')
assert(!brokerSectionSource.includes('setNewPassword(brokerSettings.data'), '券商状态不得把任何响应字段写入新密码输入')
assert(!brokerSectionSource.includes('http://') && !brokerSectionSource.includes('https://'), '券商浏览器远端不得提供猜测的默认地址')
assert(usageSource.includes('CACHE_WARNING_THRESHOLD = 0.5'), '缓存警示阈值必须严格设为 50%')
assert(usageSource.includes("CACHE_CRITICAL_THRESHOLD = 0.1"), '极低命中场景必须有 10% 临界提示')
assert(usageSource.includes("usage.data.length === 0"), '用量组件必须单独处理零数据')
assert(runtimeCss.includes('.usage-cache-health.is-warning'), '样式必须包含低命中视觉警示')
assert(runtimeCss.includes('.usage-cache-health.is-critical'), '样式必须包含极低命中视觉警示')
assert(!componentSource.includes('¥'), '契约未提供金额字段时不得自行估算人民币成本')

console.log('运行页检查通过：验证码主备链路、设置写入、模型脱敏、传输提示与用量警示均已覆盖。')
