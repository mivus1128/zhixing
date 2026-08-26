import accountFixture from '../fixtures/account/default.json' with { type: 'json' }
import accountUnavailableFixture from '../fixtures/account/unavailable.json' with { type: 'json' }
import comparisonFixture from '../fixtures/compare/mixed.json' with { type: 'json' }
import multipleProblemsFixture from '../fixtures/errors/multiple-problems.json' with { type: 'json' }
import noSuchEndpointFixture from '../fixtures/errors/no-such-endpoint.json' with { type: 'json' }
import notFoundFixture from '../fixtures/errors/not-found.json' with { type: 'json' }
import objectReadFailedFixture from '../fixtures/errors/object-read-failed.json' with { type: 'json' }
import objectWriteFailedFixture from '../fixtures/errors/object-write-failed.json' with { type: 'json' }
import confirmOrderPathIncompleteFixture from '../fixtures/instructions/confirm-order-path-incomplete.json' with { type: 'json' }
import confirmLockedFixture from '../fixtures/instructions/confirm-dry-run-locked.json' with { type: 'json' }
import emptyObjectsFixture from '../fixtures/objects/empty.json' with { type: 'json' }
import objectsFixture from '../fixtures/objects/mixed.json' with { type: 'json' }
import actionsRunFixture from '../fixtures/runs/detail-actions.json' with { type: 'json' }
import holdRunFixture from '../fixtures/runs/detail-hold-only.json' with { type: 'json' }
import longRiskRunFixture from '../fixtures/runs/detail-long-risk.json' with { type: 'json' }
import rejectedRunFixture from '../fixtures/runs/detail-rejected.json' with { type: 'json' }
import partialIssuesFixture from '../fixtures/runs/detail-partial-issues.json' with { type: 'json' }
import runIssuesFixture from '../fixtures/runs/detail-run-issues.json' with { type: 'json' }
import tradepilotLegacyFixture from '../fixtures/runs/detail-tradepilot-legacy.json' with { type: 'json' }
import defaultRunsFixture from '../fixtures/runs/list-default.json' with { type: 'json' }
import emptyRunsFixture from '../fixtures/runs/list-empty.json' with { type: 'json' }
import statusFixture from '../fixtures/status/dry-run.json' with { type: 'json' }
import neverSucceededStatusFixture from '../fixtures/status/never-succeeded.json' with { type: 'json' }
import stalledStatusFixture from '../fixtures/status/stalled.json' with { type: 'json' }
import type { ApiClient } from './client'
import type {
  AccountSummary,
  ApiFailure,
  ApiResponse,
  Instruction,
  RunComparison,
  RunListParams,
  RunSummary,
  StrategyRun,
  SystemStatus,
  TradeObject,
  TradeObjectDraft,
} from './types'
import {
  createRuntimeFixtureApi,
  type RuntimeFixtureScenario,
} from './types.runtime.ts'

const strategyRunFixtures = [
  holdRunFixture,
  actionsRunFixture,
  longRiskRunFixture,
  rejectedRunFixture,
  partialIssuesFixture,
  runIssuesFixture,
  tradepilotLegacyFixture,
] as unknown as ApiResponse<StrategyRun>[]

type FixtureScenario =
  | 'default'
  | 'stalled'
  | 'never-success'
  | 'empty-objects'
  | 'empty-runs'
  | 'mutation-error'
  | 'multiple-problems'
  | 'no-account'
  | 'no-such-endpoint'
  | 'order-path-incomplete'
  | 'error'

const fixtureScenarios = new Set<FixtureScenario>([
  'default',
  'stalled',
  'never-success',
  'empty-objects',
  'empty-runs',
  'mutation-error',
  'multiple-problems',
  'no-account',
  'no-such-endpoint',
  'order-path-incomplete',
  'error',
])

const objectStores = new Map<FixtureScenario, TradeObject[]>()

function getObjectStore(scenario: FixtureScenario): TradeObject[] {
  const existing = objectStores.get(scenario)
  if (existing) {
    return existing
  }

  const fixture = scenario === 'empty-objects' ? emptyObjectsFixture : objectsFixture
  const response = fixture as unknown as ApiResponse<TradeObject[]>
  const initial = response.ok ? structuredClone(response.data) : []
  objectStores.set(scenario, initial)
  return initial
}

function objectMutationFailure(): Promise<ApiResponse<unknown>> {
  return cloneResponse(
    objectWriteFailedFixture as unknown as ApiResponse<unknown>,
  )
}

function objectMutationSuccess(): Promise<ApiResponse<unknown>> {
  return cloneResponse({ ok: true, data: {} })
}

function objectFailure<T = unknown>(code: string, message: string): Promise<ApiResponse<T>> {
  return cloneResponse({ ok: false, error: { code, message } })
}

function buildFixtureObject(
  draft: TradeObjectDraft,
  existing?: TradeObject,
): TradeObject {
  return {
    object_id: `${draft.market}_${draft.symbol}`,
    ...draft,
    交易单位: existing?.交易单位 ?? 100,
    持仓: existing ? structuredClone(existing.持仓) : null,
    最新切片时间: existing ? existing.最新切片时间 : null,
    是否当日行情: existing?.是否当日行情 ?? false,
  }
}

function getFixtureScenario(): FixtureScenario {
  if (typeof window === 'undefined') {
    return 'default'
  }

  const scenario = new URLSearchParams(window.location.search).get('fixture')
  return fixtureScenarios.has(scenario as FixtureScenario)
    ? (scenario as FixtureScenario)
    : 'default'
}

function getRuntimeScenario(scenario: FixtureScenario): RuntimeFixtureScenario {
  if (scenario === 'error') {
    return 'runtime-error'
  }
  if (scenario === 'mutation-error') {
    return 'runtime-write-error'
  }
  if (scenario === 'stalled' || scenario === 'never-success') {
    return scenario
  }
  return 'default'
}

function cloneResponse<T>(fixture: ApiResponse<T>): Promise<ApiResponse<T>> {
  return Promise.resolve(structuredClone(fixture))
}

function filterRunSummaries(
  response: ApiResponse<RunSummary[]>,
  params: RunListParams = {},
): ApiResponse<RunSummary[]> {
  if (!response.ok) {
    return response
  }

  const from = params.from ? new Date(params.from).getTime() : Number.NEGATIVE_INFINITY
  const to = params.to ? new Date(params.to).getTime() : Number.POSITIVE_INFINITY
  const limit = Math.max(0, params.limit ?? response.data.length)
  const data = response.data
    .filter((run) => !params.system_name || run.system_name === params.system_name)
    .filter((run) => {
      const timestamp = new Date(run.生成时间).getTime()
      return timestamp >= from && timestamp <= to
    })
    .toSorted(
      (left, right) =>
        new Date(right.生成时间).getTime() - new Date(left.生成时间).getTime(),
    )
    .slice(0, limit)

  return { ok: true, data }
}

export function createFixtureClient(
  resolveScenario: () => FixtureScenario = getFixtureScenario,
): ApiClient {
  const runtimeClient = createRuntimeFixtureApi(() => getRuntimeScenario(resolveScenario()))

  return {
    ...runtimeClient,

    getStatus: () => {
      const scenario = resolveScenario()
      if (scenario === 'no-such-endpoint') {
        return cloneResponse(noSuchEndpointFixture as unknown as ApiResponse<SystemStatus>)
      }
      if (scenario === 'error') {
        return cloneResponse(notFoundFixture as unknown as ApiFailure)
      }
      if (scenario === 'stalled') {
        return cloneResponse(
          stalledStatusFixture as unknown as ApiResponse<SystemStatus>,
        )
      }
      if (scenario === 'never-success') {
        return cloneResponse(
          neverSucceededStatusFixture as unknown as ApiResponse<SystemStatus>,
        )
      }
      return cloneResponse(statusFixture as unknown as ApiResponse<SystemStatus>)
    },

    getObjects: () => {
      const scenario = resolveScenario()
      if (scenario === 'no-such-endpoint') {
        return cloneResponse(noSuchEndpointFixture as unknown as ApiResponse<TradeObject[]>)
      }
      if (scenario === 'error') {
        return cloneResponse(objectReadFailedFixture as unknown as ApiFailure)
      }
      return cloneResponse({ ok: true, data: getObjectStore(scenario) })
    },

    createObject: (draft) => {
      const scenario = resolveScenario()
      if (scenario === 'no-such-endpoint') {
        return cloneResponse(noSuchEndpointFixture as unknown as ApiResponse<unknown>)
      }
      if (scenario === 'multiple-problems') {
        return cloneResponse(multipleProblemsFixture as unknown as ApiResponse<unknown>)
      }
      if (scenario === 'error' || scenario === 'mutation-error') {
        return objectMutationFailure()
      }

      const objects = getObjectStore(scenario)
      const objectId = `${draft.market}_${draft.symbol}`
      if (objects.some((object) => object.object_id === objectId)) {
        return objectFailure('DUPLICATE_OBJECT', `标的 ${objectId} 已存在。`)
      }

      objects.push(buildFixtureObject(draft))
      return objectMutationSuccess()
    },

    updateObject: (objectId, draft) => {
      const scenario = resolveScenario()
      if (scenario === 'no-such-endpoint') {
        return cloneResponse(noSuchEndpointFixture as unknown as ApiResponse<unknown>)
      }
      if (scenario === 'multiple-problems') {
        return cloneResponse(multipleProblemsFixture as unknown as ApiResponse<unknown>)
      }
      if (scenario === 'error' || scenario === 'mutation-error') {
        return objectMutationFailure()
      }

      const objects = getObjectStore(scenario)
      const index = objects.findIndex((object) => object.object_id === objectId)
      if (index < 0) {
        return objectFailure('NOT_FOUND', `标的 ${objectId} 不存在。`)
      }

      const existing = objects[index]
      if (!existing) {
        return objectFailure('NOT_FOUND', `标的 ${objectId} 不存在。`)
      }
      const nextObjectId = `${draft.market}_${draft.symbol}`
      if (existing.object_id !== nextObjectId) {
        return objectFailure(
          'IDENTITY_IMMUTABLE',
          '市场和证券代码不能修改；要换代码请删除后重新添加。',
        )
      }
      if (
        objects.some(
          (object, candidateIndex) =>
            candidateIndex !== index && object.object_id === nextObjectId,
        )
      ) {
        return objectFailure('DUPLICATE_OBJECT', `标的 ${nextObjectId} 已存在。`)
      }

      objects[index] = buildFixtureObject(draft, existing)
      return objectMutationSuccess()
    },

    deleteObject: (objectId) => {
      const scenario = resolveScenario()
      if (scenario === 'no-such-endpoint') {
        return cloneResponse(noSuchEndpointFixture as unknown as ApiResponse<unknown>)
      }
      if (scenario === 'error' || scenario === 'mutation-error') {
        return objectMutationFailure()
      }

      const objects = getObjectStore(scenario)
      const index = objects.findIndex((object) => object.object_id === objectId)
      if (index < 0) {
        return objectFailure('NOT_FOUND', `标的 ${objectId} 不存在。`)
      }

      objects.splice(index, 1)
      return objectMutationSuccess()
    },

    getAccount: () => {
      const scenario = resolveScenario()
      if (scenario === 'no-account') {
        return cloneResponse(accountUnavailableFixture as unknown as ApiResponse<AccountSummary>)
      }
      if (scenario === 'no-such-endpoint') {
        return cloneResponse(noSuchEndpointFixture as unknown as ApiResponse<AccountSummary>)
      }
      return scenario === 'error'
        ? cloneResponse(notFoundFixture as unknown as ApiFailure)
        : cloneResponse(accountFixture as unknown as ApiResponse<AccountSummary>)
    },

    getRuns: (params) => {
      const scenario = resolveScenario()
      if (scenario === 'no-such-endpoint') {
        return cloneResponse(noSuchEndpointFixture as unknown as ApiResponse<RunSummary[]>)
      }
      if (scenario === 'error') {
        return cloneResponse(notFoundFixture as unknown as ApiFailure)
      }

      const fixture =
        scenario === 'empty-runs'
          ? (emptyRunsFixture as unknown as ApiResponse<RunSummary[]>)
          : (defaultRunsFixture as unknown as ApiResponse<RunSummary[]>)

      return cloneResponse(filterRunSummaries(fixture, params))
    },

    getRun: (strategyId) => {
      const scenario = resolveScenario()
      if (scenario === 'no-such-endpoint') {
        return cloneResponse(noSuchEndpointFixture as unknown as ApiResponse<StrategyRun>)
      }
      if (scenario === 'error') {
        return cloneResponse(notFoundFixture as unknown as ApiFailure)
      }

      const fixture = strategyRunFixtures.find(
        (candidate) => candidate.ok && candidate.data.strategy_id === strategyId,
      )

      return cloneResponse(
        fixture ?? (notFoundFixture as unknown as ApiFailure),
      )
    },

    compareRuns: () => {
      const scenario = resolveScenario()
      if (scenario === 'no-such-endpoint') {
        return cloneResponse(noSuchEndpointFixture as unknown as ApiResponse<RunComparison>)
      }
      return scenario === 'error'
        ? cloneResponse(notFoundFixture as unknown as ApiFailure)
        : cloneResponse(comparisonFixture as unknown as ApiResponse<RunComparison>)
    },

    getPendingInstructions: () => {
      const scenario = resolveScenario()
      if (scenario === 'no-such-endpoint') {
        return cloneResponse(noSuchEndpointFixture as unknown as ApiResponse<Instruction[]>)
      }
      if (scenario === 'error') {
        return objectFailure<Instruction[]>(
          'FIXTURE_PENDING_UNAVAILABLE',
          '待接管指令样例暂时无法读取，请重试。',
        )
      }
      if (scenario === 'empty-runs') {
        return cloneResponse({ ok: true, data: [] })
      }

      const response = actionsRunFixture as unknown as ApiResponse<StrategyRun>
      return cloneResponse({
        ok: true,
        data: response.ok
          ? response.data.待执行指令.filter((instruction) => instruction.状态 === 'pending')
          : [],
      })
    },

    confirmInstruction: () =>
      resolveScenario() === 'order-path-incomplete'
        ? cloneResponse(confirmOrderPathIncompleteFixture as unknown as ApiResponse<never>)
        : cloneResponse(confirmLockedFixture as unknown as ApiResponse<never>),
  }
}

export const fixtureClient = /* @__PURE__ */ createFixtureClient()
