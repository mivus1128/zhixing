import { apiErrorPresentation, type ApiError } from '../api/errors'

interface ResourceMessageProps {
  kind: 'loading' | 'empty' | 'error'
  title: string
  message: string
  onRetry?: () => void
  apiError?: ApiError | null
}

export function ResourceMessage({
  kind,
  title,
  message,
  onRetry,
  apiError,
}: ResourceMessageProps) {
  const presentation = apiError ? apiErrorPresentation(apiError) : null
  const displayKind = presentation?.kind ?? kind
  const displayTitle = presentation?.title ?? title
  const displayMessage = presentation?.message ?? message
  const canRetry = onRetry !== undefined && presentation?.retryable !== false

  return (
    <div className={`resource-message is-${displayKind}`} role={displayKind === 'error' ? 'alert' : 'status'}>
      <span aria-hidden="true">{displayKind === 'error' ? '!' : displayKind === 'empty' ? '○' : displayKind === 'notice' ? 'i' : '···'}</span>
      <div>
        <strong>{displayTitle}</strong>
        <p>{displayMessage}</p>
      </div>
      {canRetry && (
        <button type="button" onClick={onRetry}>
          重试
        </button>
      )}
    </div>
  )
}
