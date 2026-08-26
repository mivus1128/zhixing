import type {
  AssetType,
  Market,
  TradeObject,
  TradeObjectDraft,
  TradeObjectType,
} from '../api/types'

export const marketOptions = ['SH', 'SZ'] as const satisfies readonly Market[]
export const tradeObjectTypeOptions = [
  '交易标的',
  '行情对象',
] as const satisfies readonly TradeObjectType[]
export const assetTypeOptions = ['ETF', '股票'] as const satisfies readonly AssetType[]
export const duplicateTradeObjectMessage = '同一市场与代码的标的已经存在。'

export interface TradeObjectFormState {
  market: string
  symbol: string
  名称: string
  类型: string
  资产类型: string
}

export type TradeObjectDraftField = keyof TradeObjectFormState
export type TradeObjectDraftErrors = Partial<
  Record<TradeObjectDraftField, string[]>
>

export type TradeObjectDraftValidation =
  | {
      ok: true
      draft: TradeObjectDraft
      errors: TradeObjectDraftErrors
    }
  | {
      ok: false
      draft: null
      errors: TradeObjectDraftErrors
    }

export const emptyTradeObjectForm: TradeObjectFormState = {
  market: '',
  symbol: '',
  名称: '',
  类型: '',
  资产类型: '',
}

function includesValue<T extends string>(
  options: readonly T[],
  value: string,
): value is T {
  return options.includes(value as T)
}

function addError(
  errors: TradeObjectDraftErrors,
  field: TradeObjectDraftField,
  message: string,
) {
  const messages = errors[field] ?? []
  errors[field] = [...messages, message]
}

export function tradeObjectToFormState(
  object: TradeObject,
): TradeObjectFormState {
  return {
    market: object.market,
    symbol: object.symbol,
    名称: object.名称,
    类型: object.类型,
    资产类型: object.资产类型,
  }
}

export function validateTradeObjectDraft(
  input: TradeObjectFormState,
  objects: readonly TradeObject[],
  editingObjectId?: string,
): TradeObjectDraftValidation {
  const errors: TradeObjectDraftErrors = {}

  if (input.market.length === 0) {
    addError(errors, 'market', '请选择市场。')
  } else if (!includesValue(marketOptions, input.market)) {
    addError(errors, 'market', '市场只能是 SH 或 SZ。')
  }

  if (input.symbol.length === 0) {
    addError(errors, 'symbol', '请填写代码。')
  } else if (!/^\d+$/.test(input.symbol)) {
    addError(errors, 'symbol', '代码只能包含数字，空格、小数点和符号均不允许。')
  }

  if (input.名称.trim().length === 0) {
    addError(errors, '名称', '请填写名称。')
  }

  if (input.类型.length === 0) {
    addError(errors, '类型', '请选择类型。')
  } else if (!includesValue(tradeObjectTypeOptions, input.类型)) {
    addError(errors, '类型', '类型只能是交易标的或行情对象。')
  }

  if (input.资产类型.length === 0) {
    addError(errors, '资产类型', '请选择资产类型。')
  } else if (!includesValue(assetTypeOptions, input.资产类型)) {
    addError(errors, '资产类型', '资产类型只能是 ETF 或股票。')
  }

  if (
    includesValue(marketOptions, input.market) &&
    /^\d+$/.test(input.symbol) &&
    objects.some(
      (object) =>
        object.object_id !== editingObjectId &&
        object.market === input.market &&
        object.symbol === input.symbol,
    )
  ) {
    addError(errors, 'symbol', duplicateTradeObjectMessage)
  }

  if (Object.keys(errors).length > 0) {
    return { ok: false, draft: null, errors }
  }

  return {
    ok: true,
    errors,
    draft: {
      market: input.market as Market,
      symbol: input.symbol,
      名称: input.名称.trim(),
      类型: input.类型 as TradeObjectType,
      资产类型: input.资产类型 as AssetType,
    },
  }
}
