import { readFile, readdir } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createFixtureClient } from '../src/api/fixtureClient.ts'
import { validateTradeObjectDraft } from '../src/lib/objectDraft.ts'

const frontendRoot = fileURLToPath(new URL('../', import.meta.url))

function assert(condition, message) {
  if (!condition) {
    throw new Error(message)
  }
}

async function readJson(relativePath) {
  const content = await readFile(path.join(frontendRoot, relativePath), 'utf8')
  return JSON.parse(content)
}

async function sourceFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true })
  const nested = await Promise.all(
    entries.map(async (entry) => {
      const target = path.join(directory, entry.name)
      if (entry.isDirectory()) {
        return sourceFiles(target)
      }
      return /\.(ts|tsx)$/.test(entry.name) ? [target] : []
    }),
  )
  return nested.flat()
}

function assertJudgment(judgment, systemName) {
  assert(Array.isArray(judgment.理由), '理由必须始终是数组')
  assert(Array.isArray(judgment.风险), '风险必须始终是数组')
  assert(Boolean(judgment.名称), '判断名称必须由后端提供')
  assert(
    judgment.置信度 >= 0 && judgment.置信度 <= 1,
    '置信度必须处于 0 到 1 之间',
  )
  if (systemName === 'zhixing') {
    assert(
      typeof judgment.改判条件 === 'string' && judgment.改判条件.trim().length > 0,
      '知行判断必须包含非空的改判条件',
    )
  } else if (Object.hasOwn(judgment, '改判条件')) {
    assert(typeof judgment.改判条件 === 'string', '旧归档若含改判条件也必须是字符串')
  }

  const evidence = judgment.依据数据
  assert(evidence && typeof evidence === 'object', '判断必须包含依据数据')
  assert(typeof evidence.起 === 'string' && evidence.起.length > 0, '依据数据必须包含起')
  assert(typeof evidence.止 === 'string' && evidence.止.length > 0, '依据数据必须包含止')
  assert(Array.isArray(evidence.行情), '依据数据行情必须是数组')
  for (const row of evidence.行情) {
    assert(
      row !== null && typeof row === 'object' && !Array.isArray(row),
      '行情数组每一项必须是通用对象',
    )
  }
}

function assertRunIssues(run) {
  const issues = run.本轮问题
  if (run.system_name === 'zhixing') {
    assert(Array.isArray(issues), '知行归档必须显式包含本轮问题数组')
  } else if (issues === undefined) {
    return
  }

  assert(Array.isArray(issues), '本轮问题必须是数组')
  for (const issue of issues) {
    assert(issue && typeof issue === 'object', '本轮问题每一项必须是对象')
    assert(issue.object_id === null || typeof issue.object_id === 'string', '本轮问题 object_id 必须是字符串或 null')
    assert(typeof issue.code === 'string' && issue.code.length > 0, '本轮问题缺少 code')
    assert(typeof issue.message === 'string' && issue.message.length > 0, '本轮问题缺少 message')
  }
}

function assertInstruction(instruction) {
  assert(Array.isArray(instruction.拦截原因), '每条指令都必须包含拦截原因数组')
  if (instruction.状态 === 'rejected') {
    assert(instruction.拦截原因.length > 0, 'rejected 指令必须包含拦截原因')
    for (const reason of instruction.拦截原因) {
      assert(typeof reason.code === 'string' && reason.code.length > 0, '拦截原因缺少 code')
      assert(
        typeof reason.message === 'string' && reason.message.length > 0,
        '拦截原因缺少 message',
      )
    }
  } else {
    assert(instruction.拦截原因.length === 0, '非 rejected 指令的拦截原因必须为空数组')
  }
}

const [
  status,
  stalledStatus,
  neverSucceededStatus,
  account,
  accountUnavailable,
  brokerSettings,
  objects,
  emptyObjects,
  runSummaries,
  holdRun,
  actionsRun,
  rejectedRun,
  longRiskRun,
  issueRun,
  partialIssueRun,
  legacyRun,
  emptyRuns,
  requestError,
  objectReadError,
  objectWriteError,
  comparison,
  confirmLocked,
  confirmOrderPathIncomplete,
  packageJson,
] = await Promise.all([
  readJson('src/fixtures/status/dry-run.json'),
  readJson('src/fixtures/status/stalled.json'),
  readJson('src/fixtures/status/never-succeeded.json'),
  readJson('src/fixtures/account/default.json'),
  readJson('src/fixtures/account/unavailable.json'),
  readJson('src/fixtures/settings/broker.json'),
  readJson('src/fixtures/objects/mixed.json'),
  readJson('src/fixtures/objects/empty.json'),
  readJson('src/fixtures/runs/list-default.json'),
  readJson('src/fixtures/runs/detail-hold-only.json'),
  readJson('src/fixtures/runs/detail-actions.json'),
  readJson('src/fixtures/runs/detail-rejected.json'),
  readJson('src/fixtures/runs/detail-long-risk.json'),
  readJson('src/fixtures/runs/detail-run-issues.json'),
  readJson('src/fixtures/runs/detail-partial-issues.json'),
  readJson('src/fixtures/runs/detail-tradepilot-legacy.json'),
  readJson('src/fixtures/runs/list-empty.json'),
  readJson('src/fixtures/errors/not-found.json'),
  readJson('src/fixtures/errors/object-read-failed.json'),
  readJson('src/fixtures/errors/object-write-failed.json'),
  readJson('src/fixtures/compare/mixed.json'),
  readJson('src/fixtures/instructions/confirm-dry-run-locked.json'),
  readJson('src/fixtures/instructions/confirm-order-path-incomplete.json'),
  readJson('package.json'),
])

assert(status.ok === true, '系统状态 fixture 必须是成功响应')
assert(status.data.system_name === 'zhixing', '系统标识必须是 zhixing')
assert(status.data.运行模式 === 'dry_run', '三代 fixture 必须保持 dry_run')
assert(status.data.无人值守 === true, '常规状态必须覆盖无人值守开启')
assert(status.data.上一轮成功时间 !== null, '常规状态必须包含上一轮成功时间')
assert(status.data.连续失败轮数 === 0, '常规状态不得包含连续失败')
assert(status.data.最近失败原因 === null, '无失败时最近失败原因必须为 null')
assert(stalledStatus.data.连续失败轮数 >= 3, '停摆场景必须至少连续失败三轮')
assert(Boolean(stalledStatus.data.最近失败原因), '停摆场景必须包含最近失败原因')
assert(
  new Date(status.data.上一轮成功时间).getTime() -
    new Date(stalledStatus.data.上一轮成功时间).getTime() >=
    72 * 60 * 60 * 1000,
  '停摆场景的上一轮成功时间必须至少早三天',
)
assert(
  neverSucceededStatus.data.上一轮成功时间 === null,
  '从未成功场景的上一轮成功时间必须为 null',
)
assert(account.ok === true, '账户 fixture 必须是成功响应')
assert(typeof account.data.采集时间 === 'string' && account.data.采集时间.length > 0, '账户快照必须带采集时间')
assert(/^\*{3}\d{4}$/.test(account.data.账户标识), '账户标识必须保持脱敏')
for (const field of ['总资产', '可用资金', '资金余额', '冻结资金', '证券市值']) {
  assert(
    account.data[field] === null || typeof account.data[field] === 'number',
    `账户金额字段 ${field} 必须是 number 或 null`,
  )
}
assert(
  ['总资产', '可用资金', '资金余额', '冻结资金', '证券市值'].some(
    (field) => account.data[field] === null,
  ),
  '账户 fixture 必须覆盖金额未取到的 null 场景',
)
assert(Number.isInteger(account.data.持仓数量) && account.data.持仓数量 >= 0, '账户持仓数量必须是非负整数')
assert(Array.isArray(account.data.持仓列表), '账户持仓列表必须是数组')
assert(!Object.hasOwn(account.data, '账户ID'), '账户响应不得使用未受契约保护的账户ID字段')
assert(accountUnavailable.error.code === 'NO_ACCOUNT_SNAPSHOT', '账户空态必须使用 NO_ACCOUNT_SNAPSHOT')

assert(brokerSettings.ok === true, '券商配置 fixture 必须是成功响应')
assert(
  JSON.stringify(Object.keys(brokerSettings.data).sort()) ===
    JSON.stringify(['浏览器远端', '资金账号', '交易密码已配置', '缺项', '已配全'].sort()),
  '券商配置 GET 必须恰好返回五个契约字段',
)
assert(brokerSettings.data.浏览器远端.includes('.invalid'), '券商 fixture 必须使用编造的浏览器地址')
assert(brokerSettings.data.资金账号.includes('****'), '券商 fixture 资金账号必须保持遮罩')
assert(!Object.hasOwn(brokerSettings.data, '交易密码'), '券商配置 GET 绝不能包含交易密码字段')

assert(objects.ok === true && Array.isArray(objects.data), '交易标的成功体必须包含列表')
assert(
  objects.data.some(
    (item) =>
      item.类型 === '交易标的' &&
      item.持仓.是否持仓 === false &&
      item.持仓.持仓数量 === 0 &&
      item.持仓.成本价 === 0,
  ),
  '缺少无持仓正常场景',
)
assert(
  objects.data.some((item) => item.是否当日行情 === false),
  '缺少非当日行情场景',
)
assert(
  objects.data.some((item) => item.类型 === '行情对象'),
  '缺少行情对象场景',
)
const objectKeys = new Set()
for (const object of objects.data) {
  assert(['SH', 'SZ'].includes(object.market), '标的 market 只能是 SH 或 SZ')
  assert(/^\d+$/.test(object.symbol), '标的 symbol 必须是纯数字字符串')
  assert(typeof object.名称 === 'string' && object.名称.trim().length > 0, '标的名称必须非空')
  assert(
    ['交易标的', '行情对象'].includes(object.类型),
    '标的类型只能是交易标的或行情对象',
  )
  assert(['ETF', '股票'].includes(object.资产类型), '标的缺少合法资产类型')
  assert(Number.isInteger(object.交易单位) && object.交易单位 > 0, '交易单位必须是正整数')
  const objectKey = `${object.market}_${object.symbol}`
  assert(object.object_id === objectKey, 'object_id 必须等于 市场_代码')
  assert(!objectKeys.has(objectKey), '同一 市场_代码 不得重复')
  objectKeys.add(objectKey)
}
assert(
  emptyObjects.ok === true && Array.isArray(emptyObjects.data) && emptyObjects.data.length === 0,
  '空标的场景必须是成功空数组',
)

const emptyDraftValidation = validateTradeObjectDraft(
  { market: '', symbol: '', 名称: '', 类型: '', 资产类型: '' },
  objects.data,
)
assert(emptyDraftValidation.ok === false, '五项全空必须校验失败')
for (const field of ['market', 'symbol', '名称', '类型', '资产类型']) {
  assert(
    emptyDraftValidation.errors[field]?.length > 0,
    `五项全空时必须同时返回 ${field} 错误`,
  )
}

const invalidDraftValidation = validateTradeObjectDraft(
  { market: 'BJ', symbol: '51 A.1', 名称: '   ', 类型: '观察对象', 资产类型: '债券' },
  objects.data,
)
assert(invalidDraftValidation.ok === false, '非法枚举与代码必须校验失败')
for (const field of ['market', 'symbol', '名称', '类型', '资产类型']) {
  assert(
    invalidDraftValidation.errors[field]?.length > 0,
    `一轮校验必须同时返回非法字段 ${field}`,
  )
}

for (const symbol of ['51A001', '51.001', '51 001', '51-001']) {
  const validation = validateTradeObjectDraft(
    { market: 'SZ', symbol, 名称: '演示校验标的', 类型: '交易标的', 资产类型: '股票' },
    objects.data,
  )
  assert(validation.ok === false && validation.errors.symbol?.length > 0, `${symbol} 不得通过代码校验`)
}

const validDraftValidation = validateTradeObjectDraft(
  { market: 'SZ', symbol: '000001', 名称: '  演示校验标的  ', 类型: '交易标的', 资产类型: '股票' },
  objects.data,
)
assert(validDraftValidation.ok === true, '合法五字段 draft 必须通过校验')
if (validDraftValidation.ok) {
  assert(validDraftValidation.draft.symbol === '000001', '纯数字代码必须保留前导零')
  assert(validDraftValidation.draft.名称 === '演示校验标的', '名称只应清理首尾空白')
  assert(
    JSON.stringify(Object.keys(validDraftValidation.draft).sort()) ===
      JSON.stringify(['market', 'symbol', '名称', '类型', '资产类型'].sort()),
    '写请求体必须严格只含五个契约字段',
  )
}

const firstObject = objects.data[0]
const secondObject = objects.data[1]
assert(firstObject && secondObject, '标的 fixture 至少需要两项来验证重复与编辑')
const firstObjectDraft = {
  market: firstObject.market,
  symbol: firstObject.symbol,
  名称: firstObject.名称,
  类型: firstObject.类型,
  资产类型: firstObject.资产类型,
}
assert(
  validateTradeObjectDraft(firstObjectDraft, objects.data).ok === false,
  '新增同一 市场_代码 必须判定重复',
)
assert(
  validateTradeObjectDraft(firstObjectDraft, objects.data, firstObject.object_id).ok === true,
  '编辑且市场代码未变时不得把自身判定为重复',
)
assert(
  validateTradeObjectDraft(
    {
      market: secondObject.market,
      symbol: secondObject.symbol,
      名称: firstObject.名称,
      类型: firstObject.类型,
      资产类型: firstObject.资产类型,
    },
    objects.data,
    firstObject.object_id,
  ).ok === false,
  '编辑为另一条记录的 市场_代码 必须判定重复',
)
assert(
  validateTradeObjectDraft(
    { ...firstObjectDraft, market: firstObject.market === 'SH' ? 'SZ' : 'SH' },
    objects.data,
  ).ok === true,
  '代码相同但市场不同时必须允许',
)

assert(objectWriteError.ok === false, '标的写入失败 fixture 必须使用 ok:false')
assert(Boolean(objectWriteError.error.code), '标的写入失败 fixture 必须包含 code')
assert(Boolean(objectWriteError.error.message), '标的写入失败 fixture 必须包含面向人的 message')
assert(objectReadError.ok === false, '标的读取失败 fixture 必须使用 ok:false')
assert(Boolean(objectReadError.error.code), '标的读取失败 fixture 必须包含 code')
assert(Boolean(objectReadError.error.message), '标的读取失败 fixture 必须包含面向人的 message')

function assertApiFailure(response, message) {
  assert(response.ok === false, message)
  if (!response.ok) {
    assert(Boolean(response.error.code), `${message}：缺少 error.code`)
    assert(Boolean(response.error.message), `${message}：缺少 error.message`)
  }
}

const crudClient = createFixtureClient(() => 'empty-objects')
const crudDraft = {
  market: 'SZ',
  symbol: '000007',
  名称: '演示新增标的',
  类型: '交易标的',
  资产类型: '股票',
}
const beforeCreate = await crudClient.getObjects()
assert(beforeCreate.ok && beforeCreate.data.length === 0, 'CRUD 测试必须从空清单开始')
assert((await crudClient.createObject(crudDraft)).ok === true, '合法新增必须成功')
const afterCreate = await crudClient.getObjects()
assert(afterCreate.ok && afterCreate.data.length === 1, '新增成功后清单必须只增加一项')
const createdObject = afterCreate.ok ? afterCreate.data[0] : undefined
assert(createdObject?.object_id === 'SZ_000007', '新增 object_id 必须由 fixture 服务层生成')

const runtimeBeforeUpdate = createdObject
  ? structuredClone({
      交易单位: createdObject.交易单位,
      持仓: createdObject.持仓,
      最新切片时间: createdObject.最新切片时间,
      是否当日行情: createdObject.是否当日行情,
    })
  : null
assert(
  (await crudClient.updateObject('SZ_000007', { ...crudDraft, 名称: '演示修改标的' })).ok === true,
  '保持市场代码不变的修改必须成功',
)
const afterUpdate = await crudClient.getObjects()
const updatedObject = afterUpdate.ok ? afterUpdate.data[0] : undefined
assert(afterUpdate.ok && afterUpdate.data.length === 1, '修改不得新增第二条记录')
assert(updatedObject?.名称 === '演示修改标的', '修改后五字段必须反映最新值')
assert(
  JSON.stringify(
    updatedObject && {
      交易单位: updatedObject.交易单位,
      持仓: updatedObject.持仓,
      最新切片时间: updatedObject.最新切片时间,
      是否当日行情: updatedObject.是否当日行情,
    },
  ) === JSON.stringify(runtimeBeforeUpdate),
  '修改维护字段不得改写采集字段',
)

const beforeRekey = await crudClient.getObjects()
const rekeyResponse = await crudClient.updateObject('SZ_000007', {
  ...crudDraft,
  symbol: '000008',
})
assertApiFailure(rekeyResponse, '契约未定义主键迁移时 fixture 必须拒绝写入')
assert(
  JSON.stringify(await crudClient.getObjects()) === JSON.stringify(beforeRekey),
  '拒绝主键迁移后清单不得变化',
)

const duplicateClient = createFixtureClient(() => 'default')
const duplicateBefore = await duplicateClient.getObjects()
const duplicateObject = duplicateBefore.ok ? duplicateBefore.data[0] : undefined
assert(Boolean(duplicateObject), '重复测试需要默认标的')
if (duplicateObject) {
  const duplicateResponse = await duplicateClient.createObject({
    market: duplicateObject.market,
    symbol: duplicateObject.symbol,
    名称: '不同名称也不能绕过重复校验',
    类型: duplicateObject.类型,
    资产类型: duplicateObject.资产类型,
  })
  assertApiFailure(duplicateResponse, '重复 市场_代码 的新增必须失败')
}
const secondDuplicateObject = duplicateBefore.ok ? duplicateBefore.data[1] : undefined
if (duplicateObject && secondDuplicateObject) {
  assertApiFailure(
    await duplicateClient.updateObject(duplicateObject.object_id, {
      market: secondDuplicateObject.market,
      symbol: secondDuplicateObject.symbol,
      名称: duplicateObject.名称,
      类型: duplicateObject.类型,
      资产类型: duplicateObject.资产类型,
    }),
    '修改为另一条记录的 市场_代码 必须失败',
  )
}
assert(
  JSON.stringify(await duplicateClient.getObjects()) === JSON.stringify(duplicateBefore),
  '重复新增失败后清单不得变化',
)
assertApiFailure(
  await duplicateClient.updateObject('SH_999999', crudDraft),
  '修改不存在的标的必须失败',
)
assertApiFailure(
  await duplicateClient.deleteObject('SH_999999'),
  '删除不存在的标的必须失败',
)

const failureClient = createFixtureClient(() => 'mutation-error')
const failureBefore = await failureClient.getObjects()
assertApiFailure(await failureClient.createObject(crudDraft), '通用新增失败场景必须返回 ok:false')
assertApiFailure(
  await failureClient.updateObject('SH_510901', crudDraft),
  '通用修改失败场景必须返回 ok:false',
)
assertApiFailure(
  await failureClient.deleteObject('SH_510901'),
  '通用删除失败场景必须返回 ok:false',
)
assert(
  JSON.stringify(await failureClient.getObjects()) === JSON.stringify(failureBefore),
  '三种写入失败后清单都不得变化',
)

assert((await crudClient.deleteObject('SZ_000007')).ok === true, '删除现有标的必须成功')
const afterDelete = await crudClient.getObjects()
assert(afterDelete.ok && afterDelete.data.length === 0, '删除成功后必须回到空清单')
assertApiFailure(
  await crudClient.deleteObject('SZ_000007'),
  '重复删除同一标的必须失败',
)

assert(runSummaries.ok === true, '归档列表 fixture 必须是成功响应')
assert(runSummaries.data.length >= 7, '归档列表必须覆盖多轮历史、问题轮、部分产出与旧归档兼容场景')
assert(
  runSummaries.data.every(
    (summary, index, summaries) =>
      index === 0 ||
      new Date(summaries[index - 1].生成时间).getTime() >=
        new Date(summary.生成时间).getTime(),
  ),
  '归档列表必须按时间倒序',
)

assert(holdRun.ok === true, '全 hold fixture 必须是成功响应')
assert(holdRun.data.交易对象判断.length > 0, '全 hold fixture 必须包含判断')
assert(
  holdRun.data.交易对象判断.every((item) => item.操作 === 'hold'),
  '全 hold fixture 不得混入其他操作',
)
assert(
  Array.isArray(holdRun.data.待执行指令) && holdRun.data.待执行指令.length === 0,
  '全 hold fixture 的待执行指令必须是空数组',
)
assert(Array.isArray(holdRun.data.model_usage), 'model_usage 必须是数组')

const actionInstructions = actionsRun.data.待执行指令
const actions = new Set(actionInstructions.map((item) => item.action))
for (const action of ['buy', 'sell', 'cancel']) {
  assert(actions.has(action), `动作 fixture 缺少 ${action}`)
}

for (const instruction of actionInstructions) {
  if (instruction.action === 'buy' || instruction.action === 'sell') {
    assert(instruction.qty > 0, '买卖指令必须包含正数数量')
    assert(instruction.limit_price > 0, '买卖指令必须包含正数限价')
  }
  if (instruction.action === 'cancel') {
    assert(Boolean(instruction.wtbh), '撤单指令必须包含委托编号')
  }
}

const instructionCodes = actionInstructions.map((item) => item.instruction_code)
assert(
  new Set(instructionCodes).size === instructionCodes.length,
  'instruction_code 必须唯一',
)

const detailRuns = [holdRun, actionsRun, rejectedRun, longRiskRun, issueRun, partialIssueRun, legacyRun]
const allDetailInstructions = detailRuns.flatMap((run) => run.data.待执行指令)
const allDetailJudgments = detailRuns.flatMap((run) => run.data.交易对象判断)
const rejectedInstructions = allDetailInstructions.filter((item) => item.状态 === 'rejected')
assert(rejectedInstructions.length > 0, '缺少被执行校验拦下的指令场景')
assert(
  rejectedInstructions.some((instruction) => instruction.拦截原因.length >= 2),
  '至少一个被拦场景必须覆盖多条拦截原因',
)
assert(
  detailRuns.every((run) =>
    run.data.待执行指令
      .filter((instruction) => instruction.状态 === 'rejected')
      .every((instruction) =>
        run.data.交易对象判断.some(
          (judgment) => judgment.object_id === `${instruction.market}_${instruction.symbol}`,
        ),
      ),
  ),
  '被拦指令必须能关联到同轮判断',
)

const marketObjectIds = new Set(
  objects.data
    .filter((item) => item.类型 === '行情对象')
    .map((item) => `${item.market}_${item.symbol}`),
)
assert(
  [...actionInstructions, ...rejectedInstructions].every(
    (item) => !marketObjectIds.has(`${item.market}_${item.symbol}`),
  ),
  '行情对象不得出现在指令中',
)

assert(emptyRuns.ok === true, '空归档必须是成功响应')
assert(Array.isArray(emptyRuns.data) && emptyRuns.data.length === 0, '空归档必须是空数组')
assert(requestError.ok === false, '错误 fixture 必须使用 ok:false')
assert(Boolean(requestError.error.code), '错误 fixture 必须包含 code')
assert(Boolean(requestError.error.message), '错误 fixture 必须包含面向人的 message')

const longRisks = longRiskRun.data.交易对象判断.flatMap((item) => item.风险)
assert(longRisks.length === 39, '长风险 fixture 必须恰好包含 39 条风险')
assert(
  longRisks.some((item) => item.length >= 80),
  '长风险 fixture 必须包含无需手工换行的长文本',
)

for (const run of detailRuns) {
  assert(run.ok === true && run.data.context, '单轮详情必须包含 context')
  assertRunIssues(run.data)
  for (const judgment of run.data.交易对象判断) {
    assertJudgment(judgment, run.data.system_name)
  }
  for (const instruction of run.data.待执行指令) {
    assertInstruction(instruction)
  }
}

assert(issueRun.ok === true, '本轮问题 fixture 必须是成功归档')
assert(issueRun.data.交易对象判断.length === 0, '问题轮不得伪造 hold 判断')
assert(issueRun.data.待执行指令.length === 0, '问题轮不得伪造交易指令')
assert(issueRun.data.本轮问题.length >= 2, '问题轮必须覆盖标的级与整轮级问题')
assert(
  issueRun.data.本轮问题.some((issue) => issue.object_id !== null),
  '问题轮必须覆盖标的级未产出判断',
)
assert(
  issueRun.data.本轮问题.some((issue) => issue.object_id === null),
  '问题轮必须覆盖整轮级问题',
)
assert(
  issueRun.data.本轮问题.some(
    (issue) => issue.object_id !== null && issue.code === 'MISSING_TRIGGER' && issue.message.includes('改判条件'),
  ),
  '空改判条件必须以对象级 MISSING_TRIGGER 问题保留，不得伪装为 hold',
)
assert(partialIssueRun.ok === true, '部分产出问题 fixture 必须是成功归档')
assert(partialIssueRun.data.交易对象判断.length === 1, '部分产出问题 fixture 必须保留有效判断')
assert(partialIssueRun.data.待执行指令.length === 1, '部分产出问题 fixture 必须保留有效指令')
assert(partialIssueRun.data.本轮问题.length === 1, '部分产出问题 fixture 必须包含未产出原因')
assert(
  !partialIssueRun.data.交易对象判断.some(
    (judgment) => judgment.object_id === partialIssueRun.data.本轮问题[0]?.object_id,
  ),
  '未产出对象不得伪造为有效判断或 hold',
)
assert(
  !partialIssueRun.data.待执行指令.some(
    (instruction) =>
      `${instruction.market}_${instruction.symbol}` === partialIssueRun.data.本轮问题[0]?.object_id,
  ),
  '未产出对象不得伪造为交易指令',
)
assert(legacyRun.ok === true, '旧归档兼容 fixture 必须是成功响应')
assert(!Object.hasOwn(legacyRun.data, '本轮问题'), '旧归档必须覆盖本轮问题字段缺失')
assert(
  legacyRun.data.交易对象判断.every((judgment) => !Object.hasOwn(judgment, '改判条件')),
  '旧归档必须覆盖改判条件字段缺失',
)

assert(
  allDetailJudgments.some((judgment) =>
    judgment.依据数据.行情.some((row) => Object.keys(row).length > 0),
  ),
  '至少一条判断必须包含非空的通用行情记录',
)
assert(
  allDetailJudgments.some((judgment) => {
    const [firstRow, ...otherRows] = judgment.依据数据.行情
    if (!firstRow) {
      return false
    }
    const firstColumns = new Set(Object.keys(firstRow))
    return otherRows.some((row) => Object.keys(row).some((key) => !firstColumns.has(key)))
  }),
  '通用行情表必须覆盖后续行新增列的场景',
)

const detailsById = new Map(detailRuns.map((run) => [run.data.strategy_id, run.data]))
const summaryIds = runSummaries.data.map((summary) => summary.strategy_id)
assert(new Set(summaryIds).size === summaryIds.length, '归档列表 strategy_id 必须唯一')
for (const summary of runSummaries.data) {
  const detail = detailsById.get(summary.strategy_id)
  assert(Boolean(detail), `归档列表项 ${summary.strategy_id} 缺少详情 fixture`)
  assert(!('context' in summary), '归档列表项不得携带完整 context')
  assert(Array.isArray(summary.交易对象判断), '归档列表项必须包含交易对象判断')
  assert(Array.isArray(summary.待执行指令), '归档列表项必须包含待执行指令')
  assert(Array.isArray(summary.model_usage), '归档列表项必须包含 model_usage')
  assert(summary.风险控制 && typeof summary.风险控制 === 'object', '归档列表项缺少风险控制')
  assert(summary.data_window && typeof summary.data_window === 'object', '归档列表项缺少 data_window')
  assert(summary.判断条数 === summary.交易对象判断.length, '归档列表项判断计数不一致')
  assert(summary.指令条数 === summary.待执行指令.length, '归档列表项指令计数不一致')
  assertRunIssues(summary)

  const { context, ...detailWithoutContext } = detail
  void context
  const expectedKeys = [...Object.keys(detailWithoutContext), '判断条数', '指令条数'].sort()
  assert(
    JSON.stringify(Object.keys(summary).sort()) === JSON.stringify(expectedKeys),
    '归档列表项必须只比完整归档少 context，并增加两个计数字段',
  )
  for (const [key, value] of Object.entries(detailWithoutContext)) {
    assert(
      JSON.stringify(summary[key]) === JSON.stringify(value),
      `归档列表项字段 ${key} 必须与详情一致`,
    )
  }
  for (const judgment of summary.交易对象判断) {
    assertJudgment(judgment, summary.system_name)
  }
  for (const instruction of summary.待执行指令) {
    assertInstruction(instruction)
  }
}

const comparisonItems = comparison.data.对比项
assert(
  comparisonItems.some(
    (item) => item.tradepilot && item.zhixing && item.一致 === true,
  ),
  '对比 fixture 缺少一致场景',
)
assert(
  comparisonItems.some(
    (item) => item.tradepilot && item.zhixing && item.一致 === false,
  ),
  '对比 fixture 缺少不一致场景',
)
assert(
  comparisonItems.some(
    (item) => Boolean(item.tradepilot) !== Boolean(item.zhixing),
  ),
  '对比 fixture 缺少只有一方的数据场景',
)
assert(
  comparison.data.汇总.总条数 === comparisonItems.length &&
    comparison.data.汇总.一致条数 === comparisonItems.filter((item) => item.一致).length,
  '对比汇总必须与明细一致',
)

assert(confirmLocked.ok === false, 'dry-run 硬锁必须返回失败响应')
assert(
  confirmLocked.error.code === 'DRY_RUN_LOCKED',
  '确认 fixture 必须返回 DRY_RUN_LOCKED',
)
assert(confirmOrderPathIncomplete.ok === false, '未接通下单通路必须返回失败响应')
assert(
  confirmOrderPathIncomplete.error.code === 'ORDER_PATH_INCOMPLETE',
  '解锁但通路不完整时必须返回 ORDER_PATH_INCOMPLETE',
)

const fixtureErrorScenarios = [
  {
    scenario: 'no-account',
    request: (client) => client.getAccount(),
    code: 'NO_ACCOUNT_SNAPSHOT',
  },
  {
    scenario: 'no-such-endpoint',
    request: (client) => client.getStatus(),
    code: 'NO_SUCH_ENDPOINT',
  },
  {
    scenario: 'default',
    request: (client) => client.confirmInstruction('fixture-confirm'),
    code: 'DRY_RUN_LOCKED',
  },
  {
    scenario: 'order-path-incomplete',
    request: (client) => client.confirmInstruction('fixture-confirm'),
    code: 'ORDER_PATH_INCOMPLETE',
  },
]
for (const { scenario, request, code } of fixtureErrorScenarios) {
  const response = await request(createFixtureClient(() => scenario))
  assertApiFailure(response, `错误场景 ${scenario} 必须返回失败响应`)
  if (!response.ok) {
    assert(response.error.code === code, `错误场景 ${scenario} 必须映射到 ${code}`)
  }
}

assert(!('version' in packageJson), '前端 private 包不得成为业务版本的第二来源')
const dependencyVersions = Object.values({
  ...packageJson.dependencies,
  ...packageJson.devDependencies,
})
assert(
  dependencyVersions.every((version) => /^\d+\.\d+\.\d+(?:-[\w.-]+)?$/.test(version)),
  '所有依赖都必须使用精确版本号',
)

const sourceRoot = path.join(frontendRoot, 'src')
const apiEntrySource = await readFile(path.join(sourceRoot, 'api', 'index.ts'), 'utf8')
const productCssSource = await readFile(path.join(sourceRoot, 'styles', 'product.css'), 'utf8')
assert(
  /import\.meta\.env\.DEV\s*&&\s*!configuredApiBaseUrl/.test(apiEntrySource),
  'fixture 只能作为未配置 API 地址时的本地开发后备',
)
assert(
  /createHttpClient\(configuredApiBaseUrl\s*\?\?\s*['"]['"]\)/.test(apiEntrySource),
  '生产构建必须默认连接同源 /api，不得因缺少 VITE_API_BASE_URL 回退 fixture',
)
assert(
  /\.success-beacon::before\s*\{[^}]*display:\s*none/s.test(productCssSource),
  '运行状态条必须关闭旧版伪元素，避免隐式网格列撑宽页面',
)
for (const file of await sourceFiles(sourceRoot)) {
  const content = await readFile(file, 'utf8')
  const relative = path.relative(sourceRoot, file).replaceAll('\\', '/')
  if (!relative.startsWith('api/')) {
    assert(!/\bfetch\s*\(/.test(content), `${relative} 不得直接调用 fetch`)
    assert(
      !/from\s+['"][^'"]*fixtures\//.test(content),
      `${relative} 不得直接导入 fixture`,
    )
  }
  assert(!content.includes('一键全部确认'), `${relative} 不得提供批量确认入口`)
}

const appLayoutSource = await readFile(
  path.join(sourceRoot, 'layout', 'AppLayout.tsx'),
  'utf8',
)
for (const retiredNavigation of ['人工确认', '/instructions', '/runs', '/usage']) {
  assert(
    !appLayoutSource.includes(retiredNavigation),
    `主导航不得保留旧入口 ${retiredNavigation}`,
  )
}
assert(
  !appLayoutSource.includes('3.260817.00'),
  '业务版本只能来自 /api/status，不得在布局中硬编码',
)

const recentPageSource = await readFile(
  path.join(sourceRoot, 'pages', 'RecentPage.tsx'),
  'utf8',
)

function sourceSection(source, start, end, label) {
  const startIndex = source.indexOf(start)
  const endIndex = source.indexOf(end, startIndex + start.length)
  assert(startIndex >= 0 && endIndex >= 0, `无法定位 ${label} 源码片段`)
  return source.slice(startIndex, endIndex + end.length)
}

function columnHeaders(source) {
  return [...source.matchAll(/<th\b[^>]*scope="col"[^>]*>([^<]+)<\/th>/g)]
    .map((match) => match[1])
}

const recentAccountSource = sourceSection(
  recentPageSource,
  '<section className="ledger-section" aria-labelledby="account-title">',
  '</section>',
  '近况页账户区块',
)
assert(recentAccountSource.includes('NO_ACCOUNT_SNAPSHOT'), '账户缺失必须作为 NO_ACCOUNT_SNAPSHOT 空态呈现')
assert(!recentPageSource.includes('NO_ACCOUNT_SOURCE'), '前端不得继续依赖已废弃的 NO_ACCOUNT_SOURCE')
assert(recentPageSource.includes("value === null ? '未取到'"), '可空账户金额必须显式显示未取到')
assert(!recentAccountSource.includes('?? 0'), '账户金额不得把 null 兜底成零')
for (const field of ['采集时间', '账户标识', '总资产', '可用资金', '资金余额', '冻结资金', '证券市值', '持仓数量']) {
  assert(recentAccountSource.includes(`account.data.${field}`), `账户区块必须显示 ${field}`)
}

const recentTableSource = sourceSection(
  recentPageSource,
  '<table className="holdings-table"',
  '</table>',
  '近况页持仓表',
)
const recentHoldingEmptySource = sourceSection(
  recentPageSource,
  'function HoldingEmptyCell',
  'function HoldingRow',
  '近况页不适用单元格',
)
const recentHoldingRowSource = sourceSection(
  recentPageSource,
  'function HoldingRow',
  'export function RecentPage',
  '近况页持仓行',
)
assert(
  recentTableSource.includes(
    '<table className="holdings-table" role="table" aria-labelledby="holdings-title">',
  ),
  '近况页持仓表必须保留可访问名称和显式 table 角色',
)
assert(
  recentTableSource.includes('<thead role="rowgroup">') &&
    recentTableSource.includes('<tbody role="rowgroup">'),
  '近况页持仓表必须显式保留 rowgroup 角色',
)
assert(
  /<tr\b[^>]*role="row"[^>]*>/.test(recentTableSource) &&
    /<tr\b[^>]*role="row"[^>]*>/.test(recentHoldingRowSource),
  '近况页持仓表的表头行和数据行都必须显式保留 row 角色',
)
const recentColumnHeaderTags = recentTableSource.match(/<th\b[^>]*scope="col"[^>]*>/g) ?? []
assert(
  JSON.stringify(columnHeaders(recentTableSource)) ===
    JSON.stringify(['标的', '属性', '持仓 / 可用', '成本', '最新', '市值', '浮动盈亏', '数据']) &&
    recentColumnHeaderTags.every((tag) => tag.includes('role="columnheader"')),
  '近况页持仓表必须按契约保留八个显式 columnheader',
)
assert(
  /<th\b[^>]*scope="row"[^>]*role="rowheader"[^>]*>/.test(recentHoldingRowSource),
  '近况页持仓表的标的名称必须保留 rowheader 角色',
)
const recentCellTags = recentHoldingRowSource.match(/<td\b[^>]*>/g) ?? []
assert(
  recentCellTags.length === 7 &&
    recentCellTags.every((tag) => tag.includes('role="cell"')) &&
    /<td\b[^>]*role="cell"[^>]*>/.test(recentHoldingEmptySource),
  '近况页普通行的七个数据格和不适用数据格都必须显式保留 cell 角色',
)
assert(
  JSON.stringify(
    [...recentHoldingRowSource.matchAll(/<HoldingEmptyCell label="([^"]+)" \/>/g)]
      .map((match) => match[1]),
  ) === JSON.stringify(['持仓 / 可用', '成本', '市值', '浮动盈亏']),
  '近况页行情对象必须保留四个不适用数据格',
)

const objectsPageSource = await readFile(
  path.join(sourceRoot, 'pages', 'ObjectsPage.tsx'),
  'utf8',
)
const objectsTableSource = sourceSection(
  objectsPageSource,
  '<table className="objects-table"',
  '</table>',
  '标的页维护表',
)
const objectHoldingEmptySource = sourceSection(
  objectsPageSource,
  'function HoldingEmptyCell',
  'function ObjectRow',
  '标的页不适用单元格',
)
const objectRowSource = sourceSection(
  objectsPageSource,
  'function ObjectRow',
  'function DeleteObjectDialog',
  '标的页维护行',
)
const deleteDialogSource = sourceSection(
  objectsPageSource,
  'function DeleteObjectDialog',
  'export function ObjectsPage',
  '删除确认弹窗',
)
const objectColumnHeaderTags = objectsTableSource.match(/<th\b[^>]*scope="col"[^>]*>/g) ?? []
assert(
  JSON.stringify(columnHeaders(objectsTableSource)) ===
    JSON.stringify(['标的', '类型', '资产 / 单位', '持仓 / 可用', '操作']) &&
    objectColumnHeaderTags.every((tag) => tag.includes('role="columnheader"')),
  '标的页维护表必须按顺序保持五列',
)
for (const columnClass of [
  'object-column-identity',
  'object-column-type',
  'object-column-asset',
  'object-column-position',
  'object-column-actions',
]) {
  assert(
    objectsTableSource.includes(`<col className="${columnClass}" />`),
    `标的页维护表缺少固定列定义 ${columnClass}`,
  )
}
assert(
  /\.objects-table\s*\{[^}]*table-layout:\s*fixed/s.test(productCssSource) &&
    /\.objects-table \.object-row-actions\s*\{[^}]*display:\s*table-cell/s.test(productCssSource),
  '标的页桌面表头和正文必须共用固定表格列，不得让操作格脱离列计算',
)
assert(
  objectsTableSource.includes('<table className="objects-table" role="table">') &&
    objectsTableSource.includes('<thead role="rowgroup">') &&
    objectsTableSource.includes('<tbody role="rowgroup">') &&
    /<tr\b[^>]*role="row"[^>]*>/.test(objectsTableSource) &&
    /<tr\b[^>]*role="row"[^>]*>/.test(objectRowSource) &&
    /<th\b[^>]*scope="row"[^>]*role="rowheader"[^>]*>/.test(objectRowSource),
  '标的页维护表必须保留完整表格与表头角色链',
)
assert(
  !objectsTableSource.includes('成本') && !objectRowSource.includes('data-label="成本"'),
  '标的页维护表不得展示采集数据“成本”',
)
const objectRowCells = objectRowSource.match(/<td\b[^>]*>/g) ?? []
assert(
  objectRowCells.length === 4 &&
    objectRowCells.every((tag) => tag.includes('role="cell"')) &&
    /<td\b[^>]*role="cell"[^>]*>/.test(objectHoldingEmptySource) &&
    JSON.stringify(
      [...objectRowSource.matchAll(/<HoldingEmptyCell label="([^"]+)" \/>/g)]
        .map((match) => match[1]),
    ) === JSON.stringify(['持仓 / 可用']),
  '标的页普通记录和行情对象都必须保持四个显式 cell',
)
assert(
  /<dt>当前持仓<\/dt>\s*<dd>(?:(?!<\/dd>)[\s\S])*object\.持仓\.持仓数量(?:(?!<\/dd>)[\s\S])*<\/dd>/.test(
    deleteDialogSource,
  ) &&
    /<dt>当前可用<\/dt>\s*<dd>(?:(?!<\/dd>)[\s\S])*object\.持仓\.可用数量(?:(?!<\/dd>)[\s\S])*<\/dd>/.test(
      deleteDialogSource,
    ),
  '删除确认必须重复当前持仓和可用数量',
)
assert(
  /object\.类型 === '行情对象'[\s\S]*?<dt>当前持仓<\/dt><dd>不适用（行情对象不可交易）<\/dd>/.test(
    deleteDialogSource,
  ),
  '删除行情对象时必须明确说明持仓不适用',
)

const historySource = await readFile(path.join(sourceRoot, 'api', 'history.ts'), 'utf8')
assert(
  (historySource.match(/client\.getRuns\s*\(/g) ?? []).length === 1,
  '判断历史每月必须只发起一次 getRuns 请求',
)
assert(!/client\.getRun\s*\(/.test(historySource), '判断历史不得逐轮请求完整归档')

const judgmentRowSource = await readFile(
  path.join(sourceRoot, 'components', 'JudgmentRow.tsx'),
  'utf8',
)
assert(
  judgmentRowSource.includes('<dialog') &&
    judgmentRowSource.includes('dialog.showModal()') &&
    judgmentRowSource.includes('aria-haspopup="dialog"'),
  '判断详情必须使用页面级原生模态抽屉，不得退回列表行内展开',
)
assert(
  !judgmentRowSource.includes('<table') &&
    judgmentRowSource.includes('className="evidence-record-list"'),
  '判断详情不得在列表中嵌套行情表格，依据数据必须使用线性字段清单',
)
assert(
  judgmentRowSource.includes('new Set(evidence.行情.flatMap((row) => Object.keys(row)))'),
  '依据数据字段必须继续取全部行情行的列并集',
)
assert(
  judgmentRowSource.includes('if (!evidence || !Array.isArray(evidence.行情))') &&
    judgmentRowSource.includes('该轮归档未包含契约要求的依据数据'),
  '真实归档缺少依据数据时必须明确降级，不能让详情抽屉崩溃',
)
assert(
  judgmentRowSource.includes("document.documentElement.style.overflow = 'hidden'") &&
    judgmentRowSource.includes('document.documentElement.style.overflow = rootOverflow'),
  '详情抽屉打开时必须锁定背景滚动，并在关闭后恢复',
)
assert(
  /html\s*\{[^}]*scrollbar-gutter:\s*stable/s.test(productCssSource),
  '详情抽屉锁定背景滚动时必须保留页面滚动条槽，避免底层列表横向位移',
)

const frontendIgnore = await readFile(path.join(frontendRoot, '.gitignore'), 'utf8')
assert(/^node_modules\/?$/m.test(frontendIgnore), '.gitignore 必须忽略 node_modules')
assert(/^dist\/?$/m.test(frontendIgnore), '.gitignore 必须忽略 dist')

console.log('Fixture 与前端边界校验通过：v0.9 账户、券商配置、判断问题与标的维护场景均已覆盖。')
await import('./checks/compare.mjs')
await import('./checks/runtime.mjs')
await import('./checks/http-client.mjs')
