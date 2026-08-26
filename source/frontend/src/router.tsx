import { Route, Routes } from 'react-router-dom'
import { AppLayout } from './layout/AppLayout'
import { ComparePage } from './pages/ComparePage'
import { JudgmentsPage } from './pages/JudgmentsPage'
import { NotFoundPage } from './pages/NotFoundPage'
import { ObjectsPage } from './pages/ObjectsPage'
import { RecentPage } from './pages/RecentPage'
import { RuntimePage } from './pages/RuntimePage'

export function AppRoutes() {
  return (
    <Routes>
      <Route element={<AppLayout />}>
        <Route index element={<RecentPage />} />
        <Route path="judgments" element={<JudgmentsPage />} />
        <Route path="objects" element={<ObjectsPage />} />
        <Route path="runtime" element={<RuntimePage />} />
        <Route path="compare" element={<ComparePage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  )
}
