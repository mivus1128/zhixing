import { useState, type ReactNode } from 'react'
import type { CompareItem } from '../api/types.compare'
import { CompareRow } from './CompareRow'

interface CompareGroupProps {
  title: string
  description: string
  items: CompareItem[]
  tone: 'missing' | 'difference' | 'matched'
  collapsible?: boolean
  defaultOpen?: boolean
  marker?: ReactNode
}

function CompareGroupHeading({
  title,
  description,
  count,
  marker,
}: Omit<CompareGroupProps, 'items' | 'tone' | 'collapsible' | 'defaultOpen'> & {
  count: number
}) {
  return (
    <div className="compare-group-heading">
      <div>
        <strong>{title}</strong>
        <small>{description}</small>
      </div>
      {marker}
      <b>{count}</b>
    </div>
  )
}

export function CompareGroup({
  title,
  description,
  items,
  tone,
  collapsible = false,
  defaultOpen = true,
  marker,
}: CompareGroupProps) {
  const [open, setOpen] = useState(defaultOpen)
  const heading = (
    <CompareGroupHeading
      title={title}
      description={description}
      count={items.length}
      marker={marker}
    />
  )
  const rows = (
    <ol className="compare-list" aria-label={`${title}，${items.length} 条`}>
      {items.map((item, itemIndex) => (
        <CompareRow
          key={`${item.context_digest}:${item.object_id}:${itemIndex}`}
          item={item}
        />
      ))}
    </ol>
  )

  if (collapsible) {
    return (
      <details
        className={`compare-group is-${tone}`}
        open={open}
        onToggle={(event) => setOpen(event.currentTarget.open)}
      >
        <summary>{heading}</summary>
        {rows}
      </details>
    )
  }

  return (
    <section className={`compare-group is-${tone}`}>
      {heading}
      {rows}
    </section>
  )
}
