interface PageHeaderProps {
  title: string
  description: string
  aside?: string
}

export function PageHeader({ title, description, aside }: PageHeaderProps) {
  return (
    <header className="page-header">
      <div>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {aside && <span className="page-header-aside">{aside}</span>}
    </header>
  )
}
