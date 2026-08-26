import { useState } from 'react'

interface LazyJsonProps {
  label: string
  value: unknown
  className?: string
}

export function LazyJson({ label, value, className = '' }: LazyJsonProps) {
  const [open, setOpen] = useState(false)

  return (
    <details
      className={`json-disclosure ${className}`.trim()}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>{label}</summary>
      {open && <pre>{JSON.stringify(value, null, 2)}</pre>}
    </details>
  )
}
