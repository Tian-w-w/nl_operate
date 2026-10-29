import { defineConfig } from '@playwright/test'
import { resolve } from 'node:path'

// Local fixture traffic must stay on loopback even on proxy-configured machines.
process.env.NO_PROXY = [process.env.NO_PROXY, '127.0.0.1', 'localhost'].filter(Boolean).join(',')
process.env.no_proxy = process.env.NO_PROXY
const python = process.env.LLM4WEB_PYTHON || resolve('..','.venv',process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
const port = Number(process.env.LLM4WEB_TEST_PORT || 18000)
if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('Invalid LLM4WEB_TEST_PORT')
const baseURL = `http://127.0.0.1:${port}`

export default defineConfig({
  testDir: './tests/e2e',
  testMatch: '*.spec.ts',
  fullyParallel: false,
  workers: 1,
  reporter: [['list']],
  use: { baseURL, channel: process.env.PLAYWRIGHT_CHANNEL || (process.platform === 'win32' ? 'msedge' : 'chromium'), headless: true, viewport: { width: 1440, height: 1000 }, trace: 'retain-on-failure' },
  webServer: {
    command: `"${python}" -m uvicorn backend.app.main:app --host 127.0.0.1 --port ${port}`,
    cwd: '..',
    env: { DATABASE_URL: 'sqlite:///./runtime/llm4web_fixture_e2e.db', AUTH_MODE: 'demo', REFERENCE_TIME: '2026-10-26T12:00:00+08:00', MODEL_ADAPTER:'rules', MODEL_CALLS_ENABLED:'false' },
    url: `${baseURL}/api/health`,
    reuseExistingServer: false,
    timeout: 30000,
  },
})
