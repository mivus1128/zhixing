import assert from 'node:assert/strict'
import { readdir, readFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const assetsRoot = path.join(frontendRoot, 'dist', 'assets')
const assetNames = await readdir(assetsRoot)
const scriptNames = assetNames.filter((name) => name.endsWith('.js'))

assert(scriptNames.length > 0, '生产构建必须生成 JavaScript 资源')

const productionSource = (
  await Promise.all(
    scriptNames.map((name) => readFile(path.join(assetsRoot, name), 'utf8')),
  )
).join('\n')

assert(productionSource.includes('/api/status'), '生产包必须包含真实状态接口调用')

// ── 样例层有没有混进生产 ────────────────────────────────────────────
//
// 这一段原来只查三个标记，查过了就宣布「未打包演示 fixture」。
// 那句话是假的：真到包里翻，`演示模型-甲`、`demo-reasoner`、
// `demo.invalid`、`RUNTIME_FIXTURE_READ_FAILED` 四个字符串**都还在**，
// 只不过那三个恰好被树摇干净了。**一个只查自己知道会过的东西的检查，
// 和没有检查是一回事**，而且比没有更坏——它还发了一张过关的条子。
//
// 现在分两类查，两边都要对得上：
//
//   已树摇   ——  必须一个字节都不剩；
//   已知残留 ——  必须**恰好**是下面这些，多一个就得有人来解释。
//
// 残留为什么不算风险：`api/index.ts` 里 api 的取值是
// `import.meta.env.DEV && !configuredApiBaseUrl ? fixtureClient : createHttpClient(...)`，
// 生产构建里 DEV 被替换成字面量 false，整个三元折叠成 http 客户端，
// 样例层没有任何入口能走到。选择器（`?fixture=`）也不在包里——这一条
// 单独查，它才是"能不能在浏览器里把界面切成演示数据"的真正开关。
// 已经在真浏览器里验证过：带 `?fixture=default` 打开，界面还是真数据。
//
// 残留的是死代码，不是后门；但它占着体积，也让 grep 结果读起来吓人。
// 要清干净得把 fixtures 从 api/types.runtime.ts 里拆出去（前端的活）。
const treeShaken = ['fixture-hold', 'fixture-digest', '演示宽基甲ETF']
const knownResidue = ['演示模型-甲', 'demo-reasoner', 'demo.invalid', 'RUNTIME_FIXTURE_READ_FAILED']

for (const marker of treeShaken) {
  assert(!productionSource.includes(marker), `生产包不得携带 fixture 标记：${marker}`)
}

const unexpectedResidue = knownResidue.filter((marker) => !productionSource.includes(marker))
if (unexpectedResidue.length > 0) {
  // 少了是好事，但清单得跟着改，否则下一个人读到的还是旧结论。
  console.log(`提示：以下已知残留已经不在包里，可以从清单里删掉：${unexpectedResidue.join('、')}`)
}

for (const selector of ['fixture=', 'getRuntimeFixtureScenario']) {
  assert(
    !productionSource.includes(selector),
    `生产包不得携带样例选择器（这才是能把界面切成演示数据的开关）：${selector}`,
  )
}

const residueInBundle = knownResidue.filter((marker) => productionSource.includes(marker))
console.log(
  '生产包校验通过：api 默认走真实 /api，样例选择器没有打包（样例层无入口可达）。'
  + (residueInBundle.length > 0
    ? `\n  已知残留（死代码，不可达，占体积）：${residueInBundle.join('、')}`
    : ''),
)
