import { Link } from 'react-router-dom'

export function NotFoundPage() {
  return (
    <section className="not-found-page">
      <span className="eyebrow">404 / 路径未收录</span>
      <strong aria-hidden="true">空</strong>
      <h1>这里没有策略页面</h1>
      <p>当前地址不在知行工作台的路由范围内。</p>
      <Link to="/">返回近况</Link>
    </section>
  )
}
