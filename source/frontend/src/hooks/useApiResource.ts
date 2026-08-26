import { useCallback, useEffect, useState } from 'react'
import { apiErrorPresentation, type ApiError } from '../api/errors'
import type { ApiResponse } from '../api/types'

export type ApiResourceState<T> =
  | { status: 'loading'; data: null; error: null; apiError: null }
  | { status: 'success'; data: T; error: null; apiError: null }
  | { status: 'error'; data: null; error: string; apiError: ApiError }

export type ApiResource<T> = ApiResourceState<T> & {
  reload: () => void
}

export function useApiResource<T>(
  loader: () => Promise<ApiResponse<T>>,
): ApiResource<T> {
  const [reloadKey, setReloadKey] = useState(0)
  const [state, setState] = useState<ApiResourceState<T>>({
    status: 'loading',
    data: null,
    error: null,
    apiError: null,
  })

  useEffect(() => {
    let active = true
    setState({ status: 'loading', data: null, error: null, apiError: null })

    void Promise.resolve()
      .then(loader)
      .then((response) => {
        if (!active) {
          return
        }

        if (response.ok) {
          setState({ status: 'success', data: response.data, error: null, apiError: null })
          return
        }

        const presentation = apiErrorPresentation(response.error)
        setState({
          status: 'error',
          data: null,
          error: presentation.message,
          apiError: response.error,
        })
      })
      .catch(() => {
        if (active) {
          const apiError: ApiError = {
            code: 'REQUEST_FAILED',
            message: '请求未完成，请检查连接后重试。',
          }
          setState({
            status: 'error',
            data: null,
            error: apiError.message,
            apiError,
          })
        }
      })

    return () => {
      active = false
    }
  }, [loader, reloadKey])

  const reload = useCallback(() => {
    setReloadKey((current) => current + 1)
  }, [])

  return { ...state, reload }
}
