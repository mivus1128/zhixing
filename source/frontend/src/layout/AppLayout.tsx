import { useEffect, useRef } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { api, type SystemStatus } from '../api'
import {
  useApiResource,
  type ApiResource,
} from '../hooks/useApiResource'
import { describeElapsedSince } from '../lib/format'
import { getSystemHealth } from '../lib/status'

const primaryNavigation = [
  { to: '/', label: '运行总览', end: true },
  { to: '/judgments', label: '策略记录', end: false },
  { to: '/objects', label: '标的管理', end: false },
  { to: '/runtime', label: '运行管理', end: false },
] as const

const validationNavigation = [
  { to: '/compare', label: '策略对比', end: false },
] as const

const navigation = [...primaryNavigation, ...validationNavigation]

export interface AppOutletContext {
  systemStatus: ApiResource<SystemStatus>
}

function NavigationItem({ item }: { item: (typeof navigation)[number] }) {
  return (
    <NavLink
      to={item.to}
      end={item.end}
      className={({ isActive }) => `nav-item${isActive ? ' is-active' : ''}`}
    >
      <span className="nav-copy">
        <strong>{item.label}</strong>
      </span>
    </NavLink>
  )
}

export function AppLayout() {
  const { pathname } = useLocation()
  const systemStatus = useApiResource(api.getStatus)
  const mainRef = useRef<HTMLElement>(null)
  const activeItem =
    navigation.find((item) =>
      item.end ? pathname === item.to : pathname.startsWith(item.to),
    ) ?? navigation[0]!

  useEffect(() => {
    const focusTimer = window.setTimeout(() => {
      mainRef.current?.focus({ preventScroll: true })
    }, 0)
    return () => window.clearTimeout(focusTimer)
  }, [pathname])

  return (
    <div className="app-shell">
      <a
        className="skip-link"
        href="#main-content"
        onClick={(event) => {
          event.preventDefault()
          mainRef.current?.focus()
        }}
      >
        跳到主要内容
      </a>
      <aside className="sidebar">
        <div className="brand-lockup" aria-label="知行 Zhixing">
          <span className="brand-mark" aria-hidden="true">知</span>
          <span className="brand-copy">
            <strong>知行</strong>
            <small>管理控制台</small>
          </span>
        </div>

        <nav className="primary-nav" aria-label="主导航">
          {primaryNavigation.map((item) => (
            <NavigationItem key={item.to} item={item} />
          ))}
          <span className="nav-divider">分析工具</span>
          {validationNavigation.map((item) => (
            <NavigationItem key={item.to} item={item} />
          ))}
        </nav>

      </aside>

      <div className="workspace">
        <header className="topbar">
          <div className="topbar-location">
            <span className="topbar-kicker">知行</span>
            <span className="topbar-divider" aria-hidden="true">/</span>
            <strong>{activeItem.label}</strong>
          </div>
          <div className="runtime-presence" aria-live="polite">
            {systemStatus.status === 'loading' && <span>运行状态读取中</span>}
            {systemStatus.status === 'error' && <span className="is-critical">运行状态不可用</span>}
            {systemStatus.status === 'success' && (
              <>
                <span className={systemStatus.data.无人值守 ? 'is-on' : 'is-off'}>
                  无人值守 {systemStatus.data.无人值守 ? '开启' : '关闭'}
                </span>
                <span className="runtime-separator" aria-hidden="true" />
                <span>{systemStatus.data.运行模式 === 'dry_run' ? '只读验证' : '实盘模式'}</span>
                <span className={`presence-health is-${getSystemHealth(systemStatus.data)}`}>
                  上一轮成功 {systemStatus.data.上一轮成功时间
                    ? describeElapsedSince(systemStatus.data.上一轮成功时间)
                    : '尚未成功'}
                </span>
              </>
            )}
          </div>
        </header>

        <main id="main-content" ref={mainRef} className="page-stage" tabIndex={-1}>
          <Outlet context={{ systemStatus } satisfies AppOutletContext} />
        </main>
      </div>
    </div>
  )
}
