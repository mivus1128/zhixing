import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import './index.css'
import './styles/product.css'

const rootElement = document.getElementById('root')

if (!rootElement) {
  throw new Error('缺少应用挂载节点 #root')
}

createRoot(rootElement).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
