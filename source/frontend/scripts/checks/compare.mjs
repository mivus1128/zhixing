import { readdir, readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const frontendRoot = fileURLToPath(new URL('../../', import.meta.url))
const compareFixtureRoot = path.join(frontendRoot, 'src', 'fixtures', 'compare')
const allowedOperations = new Set(['buy', 'sell', 'hold', 'cancel'])

function assert(condition, message) {
  if (!condition) {
    throw new Error(message)
  }
}

async function readJson(name) {
  return JSON.parse(await readFile(path.join(compareFixtureRoot, name), 'utf8'))
}

function hasSide(item, side) {
  return item[side] !== undefined
}

function validateDecision(decision, label) {
  assert(decision && typeof decision === 'object', `${label} 必须是判断对象`)
  assert(allowedOperations.has(decision.操作), `${label} 操作不在契约枚举中`)
  assert(
    typeof decision.置信度 === 'number' && decision.置信度 >= 0 && decision.置信度 <= 1,
    `${label} 置信度必须处于 0 到 1 之间`,
  )
}

function validateSuccessFixture(name, fixture) {
  assert(fixture.ok === true, `${name} 必须使用 ok:true 成功响应包`)
  assert(fixture.data && typeof fixture.data === 'object', `${name} 缺少 data`)
  assert(Array.isArray(fixture.data.对比项), `${name} 对比项必须是数组`)

  for (const [index, item] of fixture.data.对比项.entries()) {
    const label = `${name} 第 ${index + 1} 项`
    assert(typeof item.context_digest === 'string' && item.context_digest.length > 0, `${label} 缺少 context_digest`)
    assert(typeof item.生成时间 === 'string' && Number.isFinite(Date.parse(item.生成时间)), `${label} 生成时间无效`)
    assert(typeof item.object_id === 'string' && item.object_id.length > 0, `${label} 缺少 object_id`)
    assert(typeof item.名称 === 'string' && item.名称.length > 0, `${label} 缺少名称`)
    assert(typeof item.一致 === 'boolean', `${label} 一致必须是布尔值`)
    assert(hasSide(item, 'tradepilot') || hasSide(item, 'zhixing'), `${label} 两侧不能同时缺失`)

    if (hasSide(item, 'tradepilot')) {
      validateDecision(item.tradepilot, `${label} tradepilot`)
    }
    if (hasSide(item, 'zhixing')) {
      validateDecision(item.zhixing, `${label} zhixing`)
    }
    if (hasSide(item, 'tradepilot') !== hasSide(item, 'zhixing')) {
      assert(item.一致 === false, `${label} 只有一侧时不得标为一致`)
    }
  }

  const summary = fixture.data.汇总
  const items = fixture.data.对比项
  const matchedCount = items.filter((item) => item.一致).length
  assert(summary && typeof summary === 'object', `${name} 缺少汇总`)
  assert(summary.总条数 === items.length, `${name} 总条数必须等于对比项数量`)
  assert(summary.一致条数 === matchedCount, `${name} 一致条数必须等于一致项数量`)

  if (summary.总条数 > 0) {
    const expectedRate = summary.一致条数 / summary.总条数
    assert(
      Math.abs(summary.一致率 - expectedRate) < Number.EPSILON,
      `${name} 一致率必须等于一致条数 / 总条数`,
    )
  }
}

const [allMatched, mismatched, oneSided, empty, mixed, error] = await Promise.all([
  readJson('all-matched.json'),
  readJson('mismatched.json'),
  readJson('one-sided.json'),
  readJson('empty.json'),
  readJson('mixed.json'),
  readJson('error.json'),
])

for (const [name, fixture] of [
  ['all-matched.json', allMatched],
  ['mismatched.json', mismatched],
  ['one-sided.json', oneSided],
  ['empty.json', empty],
  ['mixed.json', mixed],
]) {
  validateSuccessFixture(name, fixture)
}

assert(
  allMatched.data.对比项.length > 0 &&
    allMatched.data.对比项.every(
      (item) => hasSide(item, 'tradepilot') && hasSide(item, 'zhixing') && item.一致,
    ),
  '全一致 fixture 必须每项两侧齐全且一致',
)
assert(
  mismatched.data.对比项.some(
    (item) => hasSide(item, 'tradepilot') && hasSide(item, 'zhixing') && !item.一致,
  ),
  '不一致 fixture 必须包含两侧齐全的判断分歧',
)
assert(
  oneSided.data.对比项.every(
    (item) => hasSide(item, 'tradepilot') !== hasSide(item, 'zhixing'),
  ),
  '单边 fixture 每项必须恰好只有一侧数据',
)
assert(
  empty.data.对比项.length === 0 &&
    empty.data.汇总.总条数 === 0 &&
    empty.data.汇总.一致条数 === 0,
  '零对比项 fixture 必须是成功空态',
)

const oneSidedByObject = Map.groupBy(oneSided.data.对比项, (item) => item.object_id)
for (const [objectId, items] of oneSidedByObject) {
  const tradepilotOnly = items.find((item) => hasSide(item, 'tradepilot'))
  const zhixingOnly = items.find((item) => hasSide(item, 'zhixing'))
  assert(tradepilotOnly && zhixingOnly, `${objectId} 必须同时覆盖两种单边来源`)
  assert(
    tradepilotOnly.context_digest !== zhixingOnly.context_digest,
    `${objectId} 的两条单边记录必须使用不同 context_digest`,
  )
  assert(
    !items.some((item) => hasSide(item, 'tradepilot') && hasSide(item, 'zhixing')),
    `${objectId} 不得跨 context_digest 强行配对`,
  )
}

const mixedKinds = new Set(mixed.data.对比项.map((item) => {
  if (hasSide(item, 'tradepilot') !== hasSide(item, 'zhixing')) {
    return 'one-sided'
  }
  return item.一致 ? 'matched' : 'mismatched'
}))
assert(
  ['matched', 'mismatched', 'one-sided'].every((kind) => mixedKinds.has(kind)),
  '默认 mixed fixture 必须同时覆盖一致、判断分歧和数据缺侧',
)

assert(error.ok === false, '错误 fixture 必须使用 ok:false')
assert(typeof error.error?.code === 'string' && error.error.code.length > 0, '错误 fixture 缺少 code')
assert(typeof error.error?.message === 'string' && error.error.message.length > 0, '错误 fixture 缺少 message')

const comparePagePath = path.join(frontendRoot, 'src', 'pages', 'ComparePage.tsx')
const comparePageSource = await readFile(comparePagePath, 'utf8')
const firstPageLine = comparePageSource.split(/\r?\n/, 1)[0]
assert(
  firstPageLine.includes('验证期临时脚手架') && firstPageLine.includes('/api/runs/compare'),
  'ComparePage 顶部必须说明临时属性及接口联删要求',
)
assert(/api\.compareRuns\s*\(toApiRange\(range\)\)/.test(comparePageSource), 'ComparePage 必须带 from / to 调用 compareRuns')
assert(comparePageSource.includes("../styles/compare.css"), 'ComparePage 必须自行引入可联删的专属样式')

const componentRoot = path.join(frontendRoot, 'src', 'components')
const compareComponentNames = (await readdir(componentRoot)).filter((name) => name.startsWith('Compare'))
assert(compareComponentNames.length > 0, '对比页必须拆分 Compare* 私有组件')
for (const name of compareComponentNames) {
  const source = await readFile(path.join(componentRoot, name), 'utf8')
  assert(!/\bfetch\s*\(/.test(source), `${name} 不得直接调用 fetch`)
  assert(!/fixtures\//.test(source), `${name} 不得直接导入 fixture`)
}

console.log('Compare 专项校验通过：配对键、三态、空态、错误态与一致率公式均已覆盖。')
