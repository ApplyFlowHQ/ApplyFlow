import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const backend = fileURLToPath(new URL('../../backend/', import.meta.url))
const windows = process.platform === 'win32'
const venv = path.join(backend, '.venv', windows ? 'Scripts/python.exe' : 'bin/python')
const python = process.env.PYTHON || (existsSync(venv) ? venv : windows ? 'python' : 'python3')
const args = process.argv.slice(2).map(arg => arg === '--list' ? '--collect-only' : arg)
const suites = ['tests/e2e_workflow_check.py', '../frontend/tests/test_send_coverage.py']
const result = spawnSync(python, ['-m', 'pytest', '-q', ...suites, ...args], {
  cwd: backend,
  stdio: 'inherit',
})
if (result.error) console.error(result.error.message)
process.exitCode = result.status ?? 1
