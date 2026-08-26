const dateTimeFormatter = new Intl.DateTimeFormat('zh-CN', {
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

const timeFormatter = new Intl.DateTimeFormat('zh-CN', {
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

const moneyFormatter = new Intl.NumberFormat('zh-CN', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

const numberFormatter = new Intl.NumberFormat('zh-CN')

export function formatDateTime(value: string): string {
  return dateTimeFormatter.format(new Date(value))
}

export function formatTime(value: string): string {
  return timeFormatter.format(new Date(value))
}

export function formatMoney(value: number): string {
  return moneyFormatter.format(value)
}

export function formatNumber(value: number): string {
  return numberFormatter.format(value)
}

export function formatPrice(value: number): string {
  return value.toLocaleString('zh-CN', {
    minimumFractionDigits: 3,
    maximumFractionDigits: 3,
  })
}

export function formatPercent(value: number): string {
  return `${Math.round(value * 100)}%`
}

export function describeElapsedSince(
  value: string | null,
  now = Date.now(),
): string {
  if (value === null) {
    return '从未成功过'
  }

  const timestamp = new Date(value).getTime()
  if (!Number.isFinite(timestamp)) {
    return '时间未知'
  }

  const elapsedMinutes = Math.max(0, Math.floor((now - timestamp) / 60_000))
  if (elapsedMinutes < 1) {
    return '刚刚'
  }
  if (elapsedMinutes < 60) {
    return `${elapsedMinutes} 分钟前`
  }

  const elapsedHours = Math.floor(elapsedMinutes / 60)
  if (elapsedHours < 24) {
    return `${elapsedHours} 小时前`
  }

  return `${Math.floor(elapsedHours / 24)} 天前`
}
