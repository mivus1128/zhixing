import { useCallback } from 'react'
import { useOutletContext } from 'react-router-dom'
import {
  api,
  apiErrorPresentation,
  type RunIssue,
  type RunSummary,
  type SystemStatus,
  type TradeObject,
} from '../api'
import { PageHeader } from '../components/PageHeader'
import { ResourceMessage } from '../components/ResourceMessage'
import { useApiResource, type ApiResource } from '../hooks/useApiResource'
import type { AppOutletContext } from '../layout/AppLayout'
import {
  describeElapsedSince,
  formatDateTime,
  formatMoney,
  formatNumber,
  formatPrice,
} from '../lib/format'
import { getSystemHealth } from '../lib/status'

function formatAccountMoney(value: number | null): string {
  return value === null ? '未取到' : `¥ ${formatMoney(value)}`
}

// 后端 `runner.KNOWN_ABSENCES` 的同一份清单，**改一边必须改另一边**。
//
// 「券商没配」是**已知缺项**，不是这一轮失败：后端 preflight 一开始就
// 不把它当门槛，`round_failure()` 也明确放它过（2484cc4 改的就是这个
// —— 在那之前，只会涨的计数器把每一轮都记成失败，于是它回答不了任何
// 问题）。判「成没成功」的地方必须和后端用同一把尺，否则后端记成功、
// 界面红着说「当前可见归档内未找到成功轮次」，两边各说各的实话。
//
// ⚠️ **这份清单只放「本来就没有」，不放「本来该有却没了」。**
// 券商登录失败走的是 ACCOUNT_LOGIN_FAILED、查账户失败走
// ACCOUNT_QUERY_FAILED，都**不在**这里，所以它们照常红。
// 在拆开这两个码之前它们共用 ACCOUNT_UNAVAILABLE，于是「三次都没登进去、
// 整轮拿不到账户」在界面上和「券商还没配」长得一模一样，都是一行灰字
// —— 2026-08-21 有两轮就是这么悄悄过去的。
const KNOWN_ABSENCES = new Set(['ACCOUNT_UNAVAILABLE'])

function splitRunIssues(run: RunSummary | undefined): {
  failures: RunIssue[]
  absences: RunIssue[]
} {
  const issues = Array.isArray(run?.本轮问题) ? run.本轮问题 : []
  return {
    failures: issues.filter((issue) => !KNOWN_ABSENCES.has(issue.code)),
    absences: issues.filter((issue) => KNOWN_ABSENCES.has(issue.code)),
  }
}

function hasRunFailures(run: RunSummary): boolean {
  return splitRunIssues(run).failures.length > 0
}

function SuccessBeacon({
  resource,
  recentRuns,
}: {
  resource: ApiResource<SystemStatus>
  recentRuns: ApiResource<RunSummary[]>
}) {
  if (resource.status === 'loading') {
    return (
      <div className="success-beacon is-loading" aria-live="polite">
        <strong>上一轮成功：读取中</strong>
      </div>
    )
  }

  if (resource.status === 'error') {
    const presentation = apiErrorPresentation(resource.apiError)
    return (
      <div className="success-beacon is-critical" role="alert">
        <strong>{presentation.title ?? '上一轮成功：状态不可用'}</strong>
        <span>{presentation.message}</span>
        {presentation.retryable !== false && <button type="button" onClick={resource.reload}>重试</button>}
      </div>
    )
  }

  const status = resource.data
  const newestRun = recentRuns.status === 'success' ? recentRuns.data[0] : undefined
  const { failures: newestRunFailures, absences: newestRunAbsences } = splitRunIssues(newestRun)
  const newestRunHasFailures = newestRunFailures.length > 0
  const latestVisibleSuccess = recentRuns.status === 'success'
    ? recentRuns.data.find((run) => !hasRunFailures(run))
    : undefined
  const tone = newestRunHasFailures ? 'critical' : getSystemHealth(status)
  const failureSummary =
    newestRunHasFailures
      ? `最近一轮有 ${newestRunFailures.length} 项未产出问题`
      : status.连续失败轮数 > 0
        ? `连续失败 ${status.连续失败轮数} 轮`
        : '此后未见连续失败'
  const lastSuccess = newestRunHasFailures
    ? latestVisibleSuccess?.生成时间
    : status.上一轮成功时间
  const issueMessages = newestRunHasFailures
    ? newestRunFailures.map((issue) => issue.message).join('；')
    : null
  // 已知缺项照说不误，但**不上红**：那是「这项数据还没有」，不是「这一轮
  // 失败了」。这两件事在这个系统里从来是分开的。
  const absenceNote = newestRunAbsences.length
    ? `已知缺项：${newestRunAbsences.map((issue) => issue.message).join('；')}`
    : null

  return (
    <div className={`success-beacon is-${tone}`} aria-live="polite">
      <strong>
        上一轮成功：{lastSuccess ? describeElapsedSince(lastSuccess) : '当前可见归档内未找到成功轮次'}
      </strong>
      <span>{failureSummary}</span>
      {issueMessages || status.最近失败原因 ? (
        <span className="failure-reason">{issueMessages ?? status.最近失败原因}</span>
      ) : (
        absenceNote && <span className="absence-note">{absenceNote}</span>
      )}
    </div>
  )
}

function HoldingEmptyCell({ label }: { label: string }) {
  return (
    <td className="holding-not-applicable" data-label={label} role="cell">
      <span className="sr-only">不适用</span>
    </td>
  )
}

function HoldingUncollectedCell({ label }: { label: string }) {
  return (
    <td className="holding-uncollected" data-label={label} role="cell">
      未采集
    </td>
  )
}

function HoldingRow({ object }: { object: TradeObject }) {
  const isMarketObject = object.类型 === '行情对象'
  const position = object.持仓
  const sliceTime = object.最新切片时间
  const profitTone =
    position === null
      ? ''
      : position.浮动盈亏 > 0
        ? 'is-positive'
        : position.浮动盈亏 < 0
          ? 'is-negative'
          : ''
  const sliceUncollected = sliceTime === null

  return (
    <tr className={isMarketObject ? 'is-market-object' : ''} role="row">
      <th scope="row" data-label="标的" role="rowheader">
        <span className="object-code">{object.object_id}</span>
        <strong>{object.名称}</strong>
      </th>
      <td data-label="属性" role="cell">
        <span className={`object-kind ${isMarketObject ? 'is-reference' : ''}`}>
          {isMarketObject ? '行情参照 · 不可交易' : `${object.资产类型} · ${object.交易单位} 股/手`}
        </span>
      </td>
      {isMarketObject ? (
        <>
          <HoldingEmptyCell label="持仓 / 可用" />
          <HoldingEmptyCell label="成本" />
        </>
      ) : position === null ? (
        <>
          <HoldingUncollectedCell label="持仓 / 可用" />
          <HoldingUncollectedCell label="成本" />
        </>
      ) : (
        <>
          <td data-label="持仓 / 可用" role="cell">
            {formatNumber(position.持仓数量)} / {formatNumber(position.可用数量)}
          </td>
          <td data-label="成本" role="cell">{formatPrice(position.成本价)}</td>
        </>
      )}
      {position === null ? (
        <HoldingUncollectedCell label="最新" />
      ) : (
        <td data-label="最新" role="cell">{formatPrice(position.最新价)}</td>
      )}
      {isMarketObject ? (
        <>
          <HoldingEmptyCell label="市值" />
          <HoldingEmptyCell label="浮动盈亏" />
        </>
      ) : position === null ? (
        <>
          <HoldingUncollectedCell label="市值" />
          <HoldingUncollectedCell label="浮动盈亏" />
        </>
      ) : (
        <>
          <td data-label="市值" role="cell">{formatMoney(position.市值)}</td>
          <td className={profitTone} data-label="浮动盈亏" role="cell">
            {position.浮动盈亏 > 0 ? '+' : ''}{formatMoney(position.浮动盈亏)}
          </td>
        </>
      )}
      <td data-label="数据" role="cell">
        <span
          className={
            sliceUncollected
              ? 'freshness is-uncollected'
              : object.是否当日行情
                ? 'freshness is-fresh'
                : 'freshness is-stale'
          }
        >
          {sliceUncollected ? '未采集' : object.是否当日行情 ? '当日' : '非当日'}
        </span>
        <small>{sliceUncollected ? '—' : formatDateTime(sliceTime)}</small>
      </td>
    </tr>
  )
}

export function RecentPage() {
  const { systemStatus } = useOutletContext<AppOutletContext>()
  const account = useApiResource(api.getAccount)
  const objects = useApiResource(api.getObjects)
  const loadRecentRuns = useCallback(
    () => api.getRuns({ limit: 5, system_name: 'zhixing' }),
    [],
  )
  const recentRuns = useApiResource(loadRecentRuns)

  return (
    <section className="recent-page page-enter">
      <SuccessBeacon resource={systemStatus} recentRuns={recentRuns} />

      <PageHeader
        title="运行概览"
        description="查看最近运行记录、账户资产和持仓数据。"
        aside="数据只读"
      />

      <section className="ledger-section" aria-labelledby="recent-runs-title">
        <header className="section-heading">
          <div>
            <h2 id="recent-runs-title">最近运行记录</h2>
          </div>
          <p>零指令是正常结果，不等于系统停摆。</p>
        </header>

        {recentRuns.status === 'loading' && (
          <ResourceMessage kind="loading" title="正在读取最近判断" message="只加载归档摘要。" />
        )}
        {recentRuns.status === 'error' && (
          <ResourceMessage
            kind="error"
            title="最近判断读取失败"
            message={recentRuns.error}
            apiError={recentRuns.apiError}
            onRetry={recentRuns.reload}
          />
        )}
        {recentRuns.status === 'success' && recentRuns.data.length === 0 && (
          <ResourceMessage
            kind="empty"
            title="还没有历史判断"
            message="这是成功返回的空归档，不是请求错误。"
          />
        )}
        {recentRuns.status === 'success' && recentRuns.data.length > 0 && (
          <div className="run-ledger" role="list">
            <div className="run-ledger-head" aria-hidden="true">
              <span>时间</span>
              <span>总体判断</span>
              <span>判断</span>
              <span>有没有动手</span>
            </div>
            {recentRuns.data.map((run) => {
              // 同一把尺：已知缺项单列，不计进「N 项未产出」，也不把这一行染成
              // 出问题的样子。ACCOUNT_UNAVAILABLE 是轮次级的（object_id 为空），
              // 那一轮七个标的的判断一条不少，说它「未产出」是把话说反了。
              //
              // ⚠️ 轮次级的问题不止「已知缺项」一种：ACCOUNT_LOGIN_FAILED /
              // ACCOUNT_QUERY_FAILED 同样 object_id 为空，但它们**是故障**，
              // 要红。所以「N 项未产出」只数标的级的那些（object_id 非空），
              // 轮次级故障靠整行变红加下面那条 small 来说，不混进这个计数里。
              const { failures: runFailures, absences: runAbsences } = splitRunIssues(run)
              const hasIssues = runFailures.length > 0
              const 未产出数 = runFailures.filter((issue) => issue.object_id).length
              const hasNotes = hasIssues || runAbsences.length > 0
              const actionText = run.指令条数 === 0 ? '没有动作' : `${run.指令条数} 条指令`
              return (
              <div className={`run-summary-row${hasIssues ? ' is-run-issue' : ''}`} role="listitem" key={run.strategy_id}>
                <time dateTime={run.生成时间}>{formatDateTime(run.生成时间)}</time>
                <strong>{run.总体判断}</strong>
                <span className={hasIssues ? 'run-issue-count' : undefined}>
                  {run.判断条数} 条{未产出数 > 0 ? ` · ${未产出数} 项未产出` : ''}
                </span>
                <span className={
                  hasIssues
                    ? 'run-issue-summary'
                    : runAbsences.length > 0
                      ? 'run-absence-summary'
                      : run.指令条数 === 0 ? 'no-action' : 'has-action'
                }>
                  {hasNotes ? (
                    <>
                      <strong>{actionText}</strong>
                      {runFailures.map((issue, index) => (
                        <small key={`${issue.object_id ?? 'run'}:${issue.code}:${index}`}>
                          {issue.object_id ? '本轮未产出判断' : '本轮出问题'} · {issue.code}：{issue.message}
                        </small>
                      ))}
                      {runAbsences.map((issue, index) => (
                        <small key={`absence:${issue.object_id ?? 'run'}:${issue.code}:${index}`}>
                          已知缺项 · {issue.code}：{issue.message}
                        </small>
                      ))}
                    </>
                  ) : actionText}
                </span>
              </div>
              )
            })}
          </div>
        )}
      </section>

      <section className="ledger-section" aria-labelledby="account-title">
        <header className="section-heading">
          <div>
            <h2 id="account-title">账户概览</h2>
          </div>
        </header>

        {account.status === 'loading' && (
          <ResourceMessage kind="loading" title="正在读取账户" message="读取账户摘要中。" />
        )}
        {account.status === 'error' && account.apiError.code === 'NO_ACCOUNT_SNAPSHOT' && (
          <ResourceMessage
            kind="empty"
            title="还没有账户快照"
            message={account.error}
          />
        )}
        {account.status === 'error' && account.apiError.code !== 'NO_ACCOUNT_SNAPSHOT' && (
          <ResourceMessage
            kind="error"
            title="账户读取失败"
            message={account.error}
            apiError={account.apiError}
            onRetry={account.reload}
          />
        )}
        {account.status === 'success' && (
          <dl className="account-ledger">
            <div><dt>账户</dt><dd>{account.data.账户标识}</dd></div>
            <div><dt>总资产</dt><dd>{formatAccountMoney(account.data.总资产)}</dd></div>
            <div><dt>可用资金</dt><dd>{formatAccountMoney(account.data.可用资金)}</dd></div>
            <div><dt>资金余额</dt><dd>{formatAccountMoney(account.data.资金余额)}</dd></div>
            <div><dt>冻结资金</dt><dd>{formatAccountMoney(account.data.冻结资金)}</dd></div>
            <div><dt>证券市值</dt><dd>{formatAccountMoney(account.data.证券市值)}</dd></div>
            <div><dt>持仓数量</dt><dd>{formatNumber(account.data.持仓数量)} 项</dd></div>
            <div><dt>采集时间</dt><dd>{formatDateTime(account.data.采集时间)}</dd></div>
            {account.data.说明 && account.data.说明.length > 0 && (
              <div className="account-notes">
                <dt>采集说明</dt>
                <dd>{account.data.说明.join('；')}</dd>
              </div>
            )}
          </dl>
        )}
      </section>

      <section className="ledger-section" aria-labelledby="holdings-title">
        <header className="section-heading">
          <div>
            <h2 id="holdings-title">持仓与行情</h2>
          </div>
          <p>行情对象留在同一张清单里，但持仓列不适用。</p>
        </header>

        {objects.status === 'loading' && (
          <ResourceMessage kind="loading" title="正在读取标的" message="读取持仓和行情时效中。" />
        )}
        {objects.status === 'error' && (
          <ResourceMessage
            kind="error"
            title="标的读取失败"
            message={objects.error}
            apiError={objects.apiError}
            onRetry={objects.reload}
          />
        )}
        {objects.status === 'success' && objects.data.length === 0 && (
          <ResourceMessage
            kind="empty"
            title="标的清单为空"
            message="系统当前没有返回交易标的或行情对象；账户摘要仍可独立查看。"
          />
        )}
        {objects.status === 'success' && objects.data.length > 0 && (
          <div className="table-scroll">
            <table className="holdings-table" role="table" aria-labelledby="holdings-title">
              <thead role="rowgroup">
                <tr role="row">
                  <th scope="col" role="columnheader">标的</th>
                  <th scope="col" role="columnheader">属性</th>
                  <th scope="col" role="columnheader">持仓 / 可用</th>
                  <th scope="col" role="columnheader">成本</th>
                  <th scope="col" role="columnheader">最新</th>
                  <th scope="col" role="columnheader">市值</th>
                  <th scope="col" role="columnheader">浮动盈亏</th>
                  <th scope="col" role="columnheader">数据</th>
                </tr>
              </thead>
              <tbody role="rowgroup">
                {objects.data.map((object) => <HoldingRow key={object.object_id} object={object} />)}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </section>
  )
}
