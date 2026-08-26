import { useMemo } from 'react'
import { getCompareItemKind, type CompareItem } from '../api/types.compare'
import { CompareGroup } from './CompareGroup'

export function CompareResults({ items }: { items: CompareItem[] }) {
  const groups = useMemo(() => ({
    oneSided: items.filter((item) => getCompareItemKind(item) === 'one-sided'),
    mismatched: items.filter((item) => getCompareItemKind(item) === 'mismatched'),
    matched: items.filter((item) => getCompareItemKind(item) === 'matched'),
  }), [items])
  const exceptionCount = groups.oneSided.length + groups.mismatched.length

  return (
    <div className="compare-results">
      {groups.oneSided.length > 0 && (
        <CompareGroup
          title="数据缺侧"
          description="一套系统这一轮没跑或没配对上；它不是判断分歧。"
          items={groups.oneSided}
          tone="missing"
          marker={<span className="compare-group-marker">先查运行完整性</span>}
        />
      )}

      {groups.mismatched.length > 0 && (
        <CompareGroup
          title="判断分歧"
          description="两边拿到同一份输入，但给出了不同判断。"
          items={groups.mismatched}
          tone="difference"
          marker={<span className="compare-group-marker">基线差异</span>}
        />
      )}

      <CompareGroup
        title="一致项"
        description="默认弱化并收起，必要时再展开核对。"
        items={groups.matched}
        tone="matched"
        collapsible
        defaultOpen={exceptionCount === 0}
        marker={<span className="compare-group-marker">可整体收起</span>}
      />
    </div>
  )
}
