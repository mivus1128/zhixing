import { PageHeader } from './PageHeader'

interface PlannedPageProps {
  title: string
  description: string
  phase: string
  note: string
}

export function PlannedPage({
  title,
  description,
  phase,
  note,
}: PlannedPageProps) {
  return (
    <section className="planned-page page-enter">
      <PageHeader title={title} description={description} aside={`计划 ${phase}`} />
      <div className="planned-boundary">
        <span>{phase}</span>
        <div>
          <strong>导航位置已确定，业务内容不在 B 块展开。</strong>
          <p>{note}</p>
        </div>
      </div>
    </section>
  )
}
