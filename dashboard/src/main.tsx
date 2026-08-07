import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import App from './App'
import './styles.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // One retry, not the default three. A control-plane poll that fails is
      // information, and burying it under retries would delay the banner by
      // several seconds during exactly the incident you are watching for.
      retry: 1,
      refetchOnWindowFocus: false,
      staleTime: 0,
    },
  },
})

const container = document.getElementById('root')
if (!container) throw new Error('#root not found')

createRoot(container).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
)
