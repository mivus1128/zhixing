import type { SystemStatus } from '../api'

export type HealthTone = 'healthy' | 'warning' | 'critical'

export function getSystemHealth(
  status: SystemStatus,
  now = Date.now(),
): HealthTone {
  if (status.上一轮成功时间 === null || status.连续失败轮数 >= 3) {
    return 'critical'
  }

  const elapsed = now - new Date(status.上一轮成功时间).getTime()
  if (!Number.isFinite(elapsed) || elapsed > 24 * 60 * 60 * 1000) {
    return 'critical'
  }

  return status.连续失败轮数 > 0 ? 'warning' : 'healthy'
}
