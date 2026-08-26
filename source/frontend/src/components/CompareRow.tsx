import type {
  CompareDecision,
  CompareItem,
} from '../api/types.compare'
import { getCompareItemKind } from '../api/types.compare'
import { formatDateTime } from '../lib/format'

const operationLabels = {
  buy: '买入',
  sell: '卖出',
  hold: '持有',
  cancel: '撤单',
} as const

const kindLabels = {
  matched: '一致',
  mismatched: '判断分歧',
  'one-sided': '数据缺侧',
} as const

function shortenDigest(digest: string): string {
  return digest.length > 30
    ? `${digest.slice(0, 17)}…${digest.slice(-9)}`
    : digest
}

function CompareDecisionCell({
  label,
  decision,
}: {
  label: '二代' | '三代'
  // 一律传,值可以是 undefined —— 「这一侧没有数据」是对比页要显示的三态之一,
  // 不是"这个属性可以省略"。`exactOptionalPropertyTypes` 下两者不是一回事。
  decision: CompareDecision | undefined
}) {
  if (!decision) {
    return (
      <div className="compare-decision is-missing">
        <span>{label}</span>
        <strong>该侧无数据</strong>
        <small>这一轮没有可配对的输出</small>
      </div>
    )
  }

  return (
    <div className={`compare-decision operation-${decision.操作}`}>
      <span>{label}</span>
      <strong>{operationLabels[decision.操作]}</strong>
      <small>{Math.round(decision.置信度 * 100)}% 置信度</small>
    </div>
  )
}

export function CompareRow({ item }: { item: CompareItem }) {
  const kind = getCompareItemKind(item)

  return (
    <li className={`compare-row is-${kind}`}>
      <div className="compare-row-identity">
        <time dateTime={item.生成时间}>{formatDateTime(item.生成时间)}</time>
        <div>
          <code>{item.object_id}</code>
          <strong>{item.名称}</strong>
        </div>
        <span className="compare-row-state">{kindLabels[kind]}</span>
      </div>

      <div className="compare-pair-key">
        <span>同源输入</span>
        <code title={item.context_digest}>{shortenDigest(item.context_digest)}</code>
        <small>context_digest</small>
      </div>

      <div className="compare-decisions">
        <CompareDecisionCell label="二代" decision={item.tradepilot} />
        <span className="compare-versus" aria-hidden="true">VS</span>
        <CompareDecisionCell label="三代" decision={item.zhixing} />
      </div>
    </li>
  )
}
