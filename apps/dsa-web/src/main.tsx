import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { ThemeProvider } from './components/theme/ThemeProvider'
import { reloadForFreshBundle } from './utils/chunkReload'

// Vite raises this when a dynamic import of a chunk fails — most often because
// the bundle was rebuilt while this page was open, leaving the loaded manifest
// pointing at filenames that no longer exist. Recover by reloading once.
window.addEventListener('vite:preloadError', (event) => {
  if (reloadForFreshBundle()) {
    event.preventDefault()
  }
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ThemeProvider>
      <App />
    </ThemeProvider>
  </StrictMode>,
)
