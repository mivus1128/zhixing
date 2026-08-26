import { HashRouter } from 'react-router-dom'
import { AppRoutes } from './router'

export default function App() {
  return (
    <HashRouter>
      <AppRoutes />
    </HashRouter>
  )
}
