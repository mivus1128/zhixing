export interface ApiProblem {
  code: string
  message: string
}

export interface ApiError extends ApiProblem {
  问题?: ApiProblem[]
}

export interface ApiErrorPresentation {
  kind: 'error' | 'notice'
  title?: string
  message: string
  retryable?: boolean
}

export function apiErrorPresentation(error: ApiError): ApiErrorPresentation {
  switch (error.code) {
    case 'NO_SUCH_ENDPOINT':
      return {
        kind: 'error',
        title: '接口版本不匹配',
        message: `前端请求的路由不存在，说明前后端版本不匹配。请联系前端维护者检查路由。后端说明：${error.message}`,
        retryable: false,
      }
    case 'DRY_RUN_LOCKED':
      return {
        kind: 'notice',
        title: '只读验证模式',
        message: '系统处于只读演练态，不会真的下单。',
        retryable: false,
      }
    case 'ORDER_PATH_INCOMPLETE':
      return {
        kind: 'notice',
        title: '下单通路尚未接通',
        message: error.message,
        retryable: false,
      }
    default:
      return { kind: 'error', message: error.message }
  }
}

export function apiErrorMessages(error: ApiError): string[] {
  return error.问题 && error.问题.length > 0
    ? error.问题.map((problem) => problem.message)
    : [apiErrorPresentation(error).message]
}
