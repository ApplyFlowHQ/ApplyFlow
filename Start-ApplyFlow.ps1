<#
.SYNOPSIS
    Bootstraps and launches ApplyFlow Global without manual .env editing.
.DESCRIPTION
    Supports isolated Docker Compose deployment (default when Docker is available)
    and an explicit single-host Local Python/Vite fallback (-Mode Local).
    Persists host-protected encryption secrets outside the database, checks key integrity,
    applies Alembic migrations, displays the one-time setup claim code in the local
    operator console only, and opens the setup wizard URL.
#>
[CmdletBinding()]
param(
    [ValidateSet("Auto", "Docker", "Local")]
    [string]$Mode = "Auto",

    [switch]$NewSetupCode,
    [switch]$RecoverOwner,
    [switch]$NoBrowser,
    [switch]$ReconfigurePorts,
    [switch]$Stop,
    [switch]$NonInteractive
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendDir = Join-Path $ProjectRoot "backend"
$FrontendDir = Join-Path $ProjectRoot "frontend"
$DeployDir = Join-Path $ProjectRoot "deploy"
$BootstrapDir = Join-Path $ProjectRoot ".bootstrap"
$SecretsDir = Join-Path $BootstrapDir "secrets"
$InstallMetaPath = Join-Path $BootstrapDir "install.json"
$RuntimeEnvPath = Join-Path $BootstrapDir "runtime.env"
$OverrideComposePath = Join-Path $BootstrapDir "docker-compose.override.yml"
$LocalPidsPath = Join-Path $BootstrapDir "local_pids.json"
$DataDir = Join-Path $ProjectRoot "data"
$StorageDir = Join-Path $ProjectRoot "storage\private"
$LogsDir = Join-Path $ProjectRoot "logs"
$ComposeProjectName = "applyflow-global"

function Write-Info([string]$Msg) {
    Write-Host "[ApplyFlow] $Msg" -ForegroundColor Cyan
}

function Write-Warn([string]$Msg) {
    Write-Host "[ApplyFlow] $Msg" -ForegroundColor Yellow
}

function Write-Err([string]$Msg) {
    Write-Host "[ApplyFlow ERROR] $Msg" -ForegroundColor Red
}

function New-RandomUrlSafeBytes([int]$ByteLength) {
    $bytes = New-Object byte[] $ByteLength
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    } finally {
        $rng.Dispose()
    }
    return [Convert]::ToBase64String($bytes).Replace("+", "-").Replace("/", "_")
}

function New-RandomPassword([int]$ByteLength = 24) {
    return (New-RandomUrlSafeBytes -ByteLength $ByteLength).TrimEnd("=")
}

function Set-RestrictedDirectoryAcl([string]$PathToProtect) {
    if (-not (Test-Path $PathToProtect)) {
        New-Item -ItemType Directory -Path $PathToProtect -Force | Out-Null
    }
    try {
        $currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        & icacls $PathToProtect /inheritance:r /grant:r "${currentIdentity}:(OI)(CI)F" "SYSTEM:(OI)(CI)F" | Out-Null
    } catch {
        Write-Warn "Could not tighten Windows ACL on $PathToProtect via icacls: $($_.Exception.Message)"
    }
}

function Write-SecretFileUtf8NoBom([string]$Path, [string]$Value) {
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Value, $utf8NoBom)
    try {
        $currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        & icacls $Path /inheritance:r /grant:r "${currentIdentity}:F" "SYSTEM:F" | Out-Null
    } catch {
        Write-Warn "Could not tighten file ACL on $Path"
    }
}

function Test-TcpPortFree([int]$Port) {
    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($null -ne $listener) {
            try { $listener.Stop() } catch {}
        }
    }
}

function Find-FreePort([int[]]$PreferredPorts) {
    foreach ($p in $PreferredPorts) {
        if (Test-TcpPortFree -Port $p) {
            return $p
        }
    }
    throw "No free port available among candidates: $($PreferredPorts -join ', ')"
}

function Get-DotEnvValue([string]$EnvPath, [string]$Key) {
    if (-not (Test-Path $EnvPath)) { return "" }
    foreach ($line in Get-Content -Path $EnvPath -ErrorAction SilentlyContinue) {
        if ($line -match "^\s*#") { continue }
        if ($line -match "^\s*$([regex]::Escape($Key))\s*=\s*(.*)$") {
            return $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
    return ""
}

function Test-DockerAvailable {
    $cmd = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $cmd) {
        return @{ Available = $false; Reason = "Docker CLI ('docker') is not installed or not on PATH." }
    }
    try {
        $null = & docker info --format "{{.ServerVersion}}" 2>$null
        if ($LASTEXITCODE -ne 0) {
            return @{ Available = $false; Reason = "Docker CLI is installed, but the Docker Desktop / Engine daemon is not running." }
        }
        $null = & docker compose version 2>$null
        if ($LASTEXITCODE -ne 0) {
            return @{ Available = $false; Reason = "'docker compose' v2 plugin is unavailable." }
        }
        return @{ Available = $true; Reason = "" }
    } catch {
        return @{ Available = $false; Reason = "Unable to communicate with Docker Engine: $($_.Exception.Message)" }
    }
}

function Get-PersistedMeta {
    if (Test-Path $InstallMetaPath) {
        try {
            return (Get-Content $InstallMetaPath -Raw | ConvertFrom-Json)
        } catch {
            throw "Corrupt bootstrap metadata at $InstallMetaPath. Inspect or restore before continuing."
        }
    }
    return $null
}

function Save-PersistedMeta([hashtable]$Meta) {
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $json = $Meta | ConvertTo-Json -Depth 6
    [System.IO.File]::WriteAllText($InstallMetaPath, $json, $utf8NoBom)
}

function Ensure-BootstrapSecrets([bool]$DatabaseDataAlreadyExists, [string]$ExistingEnvFile) {
    Set-RestrictedDirectoryAcl -PathToProtect $SecretsDir
    $encKeyPath = Join-Path $SecretsDir "mailbox_encryption.key"
    $appSecretPath = Join-Path $SecretsDir "app_secret.key"
    $pgPassPath = Join-Path $SecretsDir "postgres_password"

    $envEncKey = Get-DotEnvValue -EnvPath $ExistingEnvFile -Key "MAILBOX_ENCRYPTION_KEY"
    $envAppSecret = Get-DotEnvValue -EnvPath $ExistingEnvFile -Key "APP_SECRET_KEY"
    $envPgPass = Get-DotEnvValue -EnvPath $ExistingEnvFile -Key "POSTGRES_PASSWORD"

    if (-not (Test-Path $encKeyPath)) {
        if ($envEncKey) {
            Write-SecretFileUtf8NoBom -Path $encKeyPath -Value $envEncKey
        } elseif ($DatabaseDataAlreadyExists) {
            throw "RECOVERY PROBLEM: Existing ApplyFlow data was detected, but $encKeyPath is missing and no MAILBOX_ENCRYPTION_KEY was found in .env. Refusing to generate a replacement key because stored credentials would become undecryptable."
        } else {
            Write-SecretFileUtf8NoBom -Path $encKeyPath -Value (New-RandomUrlSafeBytes -ByteLength 32)
        }
    }

    if (-not (Test-Path $appSecretPath)) {
        if ($envAppSecret -and $envAppSecret -notlike "*change-me*") {
            Write-SecretFileUtf8NoBom -Path $appSecretPath -Value $envAppSecret
        } else {
            Write-SecretFileUtf8NoBom -Path $appSecretPath -Value (New-RandomUrlSafeBytes -ByteLength 48)
        }
    }

    if (-not (Test-Path $pgPassPath)) {
        if ($envPgPass -and $envPgPass -ne "change_me_in_env") {
            Write-SecretFileUtf8NoBom -Path $pgPassPath -Value $envPgPass
        } else {
            Write-SecretFileUtf8NoBom -Path $pgPassPath -Value (New-RandomPassword -ByteLength 24)
        }
    }

    return @{
        EncryptionKeyFile = $encKeyPath
        AppSecretFile = $appSecretPath
        PostgresPasswordFile = $pgPassPath
    }
}

function Wait-PortFree([int]$Port, [int]$TimeoutSeconds = 5) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-TcpPortFree -Port $Port) { return $true }
        Start-Sleep -Milliseconds 250
    }
    return (Test-TcpPortFree -Port $Port)
}

function Stop-LocalProcesses {
    if (Test-Path $LocalPidsPath) {
        $stoppedAny = $false
        $recordedPorts = @()
        try {
            $pids = Get-Content $LocalPidsPath -Raw | ConvertFrom-Json
            if ($pids.PSObject.Properties["backend_port"]) { $recordedPorts += [int]$pids.backend_port }
            if ($pids.PSObject.Properties["frontend_port"]) { $recordedPorts += [int]$pids.frontend_port }
            foreach ($prop in @("backend_pid", "frontend_pid")) {
                $procId = $pids.$prop
                if ($procId) {
                    $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
                    if ($proc) {
                        Write-Info "Stopping local process tree PID $procId..."
                        $null = & taskkill.exe /PID $procId /T /F 2>$null
                        $stoppedAny = $true
                    }
                }
            }
        } catch {}
        Remove-Item $LocalPidsPath -Force -ErrorAction SilentlyContinue
        if ($stoppedAny) {
            foreach ($p in $recordedPorts) {
                $null = Wait-PortFree -Port $p -TimeoutSeconds 5
            }
        }
    }
}

function Show-ClaimOutput([string]$JsonLine, [string]$SetupUrl) {
    $parsed = $null
    try {
        $parsed = $JsonLine.Trim() | ConvertFrom-Json
    } catch {
        Write-Warn "Unexpected bootstrap token output: $JsonLine"
        return $false
    }

    Write-Host ""
    if ($parsed.status -eq "token_issued") {
        Write-Host "========================================================================" -ForegroundColor Green
        Write-Host " APPLYFLOW GLOBAL - ONE-TIME INSTALLATION OWNER SETUP CODE" -ForegroundColor Green
        Write-Host " Setup Wizard URL : $SetupUrl" -ForegroundColor White
        Write-Host " One-Time Code    : $($parsed.setup_code)" -ForegroundColor Yellow
        Write-Host " Expires At (UTC) : $($parsed.expires_at)" -ForegroundColor Gray
        Write-Host " Note: Stored only as a one-way SHA-256 hash; shown here once only." -ForegroundColor Gray
        Write-Host "========================================================================" -ForegroundColor Green
        Write-Host ""
        return $true
    } elseif ($parsed.status -eq "active_token_exists") {
        Write-Warn "Installation ownership claim is pending (an active setup code was already issued)."
        Write-Warn "If you lost that code, rerun with: .\Start-ApplyFlow.ps1 -NewSetupCode"
        return $true
    } elseif ($parsed.status -eq "existing_operator") {
        Write-Info "Existing administrator account detected; sign in at the web UI or pass -RecoverOwner to issue a recovery code."
        return $false
    }
    return $false
}

function Wait-HttpHealthy([string]$HealthUrl, [int]$TimeoutSeconds = 90) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-RestMethod -Uri $HealthUrl -Method Get -TimeoutSec 3 -ErrorAction Stop
            if ($resp.status -eq "ok") {
                return $true
            }
        } catch {}
        Start-Sleep -Seconds 2
    }
    return $false
}

function Open-BrowserUrl([string]$Url) {
    if ($NoBrowser) {
        Write-Info "Open ApplyFlow in your browser: $Url"
        return
    }
    try {
        Start-Process $Url | Out-Null
        Write-Info "Opened browser at $Url"
    } catch {
        Write-Info "Automatic browser launch unavailable. Open this URL manually: $Url"
    }
}

# -----------------------------------------------------------------------------
# Ensure runtime directories exist
# -----------------------------------------------------------------------------
foreach ($dir in @($BootstrapDir, $DataDir, $StorageDir, $LogsDir)) {
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
}

$existingEnvPath = Join-Path $ProjectRoot ".env"
$persisted = Get-PersistedMeta

if ($Stop) {
    Stop-LocalProcesses
    $dockerCheck = Test-DockerAvailable
    if ($dockerCheck.Available) {
        $proj = if ($persisted -and $persisted.compose_project_name) { $persisted.compose_project_name } else { $ComposeProjectName }
        $composeArgs = @("compose", "-p", $proj, "-f", (Join-Path $DeployDir "docker-compose.yml"))
        if (Test-Path $OverrideComposePath) { $composeArgs += @("-f", $OverrideComposePath) }
        $composeArgs += "down"
        Write-Info "Stopping Docker Compose stack '$proj'..."
        & docker @composeArgs
    }
    Write-Info "ApplyFlow stopped cleanly (volumes and databases preserved)."
    exit 0
}

# Resolve target mode cleanly
$dockerStatus = Test-DockerAvailable
if ($Mode -eq "Docker" -and -not $dockerStatus.Available) {
    Write-Err $dockerStatus.Reason
    Write-Err "Install/start Docker Desktop to use Docker deployment, or run '.\Start-ApplyFlow.ps1 -Mode Local' for local Python+Vite startup."
    exit 1
}

if ($Mode -eq "Auto") {
    if ($dockerStatus.Available) {
        $Mode = "Docker"
    } else {
        Write-Warn "Docker prerequisite check failed: $($dockerStatus.Reason)"
        if ($NonInteractive -or -not [Environment]::UserInteractive) {
            Write-Err "Non-interactive run requires either running Docker Engine or explicit '-Mode Local'."
            exit 1
        }
        $choice = Read-Host "Press [L] to start in Local single-host mode (Python+Vite) or [Q] to quit and start Docker"
        if ($choice -match "^[Ll]$") {
            $Mode = "Local"
        } else {
            Write-Err "Aborted. Start Docker Desktop or rerun with '.\Start-ApplyFlow.ps1 -Mode Local'."
            exit 1
        }
    }
}

# =============================================================================
# MODE: DOCKER COMPOSE (PostgreSQL + Redis + Celery Worker/Beat + Caddy)
# =============================================================================
if ($Mode -eq "Docker") {
    $projectName = if ($persisted -and $persisted.compose_project_name) { $persisted.compose_project_name } else { $ComposeProjectName }

    # Detect existing compose volumes to avoid silently replacing encryption keys if secrets vanished
    $volOutput = & docker volume ls --filter "name=${projectName}_pg_data" --format "{{.Name}}" 2>$null
    $hasExistingPgVolume = [bool]($volOutput | Where-Object { $_ -eq "${projectName}_pg_data" })
    $secretFiles = Ensure-BootstrapSecrets -DatabaseDataAlreadyExists $hasExistingPgVolume -ExistingEnvFile $existingEnvPath

    $httpPort = if ($persisted -and $persisted.http_port -and -not $ReconfigurePorts) {
        [int]$persisted.http_port
    } else {
        Find-FreePort -PreferredPorts @(80, 8080, 8081, 8880, 8888)
    }
    $httpsPort = if ($persisted -and $persisted.https_port -and -not $ReconfigurePorts) {
        [int]$persisted.https_port
    } else {
        Find-FreePort -PreferredPorts @(443, 8443, 8444, 9443)
    }

    # Verify persisted port is not hijacked by another local service if stack isn't already up
    $runningWeb = & docker compose -p $projectName ps -q caddy 2>$null
    if (-not $runningWeb -and -not (Test-TcpPortFree -Port $httpPort)) {
        Write-Err "Configured port $httpPort is currently occupied by another program, and changing it silently would invalidate OAuth redirect URLs. Free port $httpPort or pass -ReconfigurePorts."
        exit 1
    }

    $baseUrl = if ($httpPort -eq 80) { "http://localhost" } else { "http://localhost:$httpPort" }
    $apiPublicUrl = $baseUrl

    Save-PersistedMeta @{
        mode = "docker"
        compose_project_name = $projectName
        http_port = $httpPort
        https_port = $httpsPort
        app_url = $baseUrl
        api_public_url = $apiPublicUrl
        updated_at = (Get-Date).ToUniversalTime().ToString("o")
    }

    $runtimeLines = @(
        "ENVIRONMENT=production",
        "APP_URL=$baseUrl",
        "API_PUBLIC_URL=$apiPublicUrl",
        "ALLOWED_ORIGINS=$baseUrl",
        "GOOGLE_OAUTH_REDIRECT_URI_AUTH=$apiPublicUrl/api/v1/auth/google/callback",
        "GOOGLE_OAUTH_REDIRECT_URI_GMAIL=$apiPublicUrl/api/v1/mailboxes/gmail/callback",
        "GOOGLE_OAUTH_REDIRECT_URI_SHEETS=$apiPublicUrl/api/v1/sheets/callback",
        "MS_GRAPH_REDIRECT_URI=$apiPublicUrl/api/v1/mailboxes/ms-graph/callback",
        "COOKIE_SECURE=false",
        "LIVE_SENDING_ENABLED=false",
        "DATABASE_URL=postgresql+psycopg://applyflow:__POSTGRES_PASSWORD__@postgres:5432/applyflow",
        "REDIS_URL=redis://redis:6379/0",
        "CELERY_BROKER_URL=redis://redis:6379/1",
        "CELERY_RESULT_BACKEND=redis://redis:6379/2",
        "CELERY_TASK_ALWAYS_EAGER=false",
        "APPLYFLOW_HTTP_PORT=$httpPort",
        "APPLYFLOW_HTTPS_PORT=$httpsPort",
        "APPLYFLOW_CADDYFILE=./Caddyfile.local",
        "APPLYFLOW_ENV_FILE=../.bootstrap/runtime.env",
        "COMPOSE_PROJECT_NAME=$projectName"
    )
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($RuntimeEnvPath, ($runtimeLines -join "`n"), $utf8NoBom)

    $secMount = ($SecretsDir -replace "\\", "/")
    # Mount secrets ONLY into database/backend/worker services; NEVER into redis, frontend, caddy; NEVER mount Docker socket.
    $overrideYaml = @"
services:
  postgres:
    environment:
      POSTGRES_PASSWORD: ""
      POSTGRES_PASSWORD_FILE: /run/secrets/applyflow/postgres_password
    volumes:
      - ${secMount}/postgres_password:/run/secrets/applyflow/postgres_password:ro

  migrate:
    environment:
      MAILBOX_ENCRYPTION_KEY_FILE: /run/secrets/applyflow/mailbox_encryption.key
      APP_SECRET_KEY_FILE: /run/secrets/applyflow/app_secret.key
      POSTGRES_PASSWORD_FILE: /run/secrets/applyflow/postgres_password
    volumes:
      - ${secMount}:/run/secrets/applyflow:ro

  backend:
    environment:
      MAILBOX_ENCRYPTION_KEY_FILE: /run/secrets/applyflow/mailbox_encryption.key
      APP_SECRET_KEY_FILE: /run/secrets/applyflow/app_secret.key
      POSTGRES_PASSWORD_FILE: /run/secrets/applyflow/postgres_password
    volumes:
      - ${secMount}:/run/secrets/applyflow:ro

  worker:
    environment:
      MAILBOX_ENCRYPTION_KEY_FILE: /run/secrets/applyflow/mailbox_encryption.key
      APP_SECRET_KEY_FILE: /run/secrets/applyflow/app_secret.key
      POSTGRES_PASSWORD_FILE: /run/secrets/applyflow/postgres_password
    volumes:
      - ${secMount}:/run/secrets/applyflow:ro

  beat:
    environment:
      MAILBOX_ENCRYPTION_KEY_FILE: /run/secrets/applyflow/mailbox_encryption.key
      APP_SECRET_KEY_FILE: /run/secrets/applyflow/app_secret.key
      POSTGRES_PASSWORD_FILE: /run/secrets/applyflow/postgres_password
    volumes:
      - ${secMount}:/run/secrets/applyflow:ro
"@
    [System.IO.File]::WriteAllText($OverrideComposePath, $overrideYaml, $utf8NoBom)

    $composeBase = Join-Path $DeployDir "docker-compose.yml"
    Write-Info "Starting PostgreSQL and Redis for project '$projectName'..."
    & docker compose --env-file $RuntimeEnvPath -p $projectName -f $composeBase -f $OverrideComposePath up -d postgres redis
    if ($LASTEXITCODE -ne 0) { throw "Failed to start PostgreSQL/Redis containers." }

    Write-Info "Checking encryption key integrity..."
    & docker compose --env-file $RuntimeEnvPath -p $projectName -f $composeBase -f $OverrideComposePath run --rm -T migrate python -m app.cli_bootstrap check-encryption-integrity
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Encryption integrity check failed. Refusing to regenerate keys or start with mismatched secrets."
        exit 2
    }

    Write-Info "Applying database migrations..."
    & docker compose --env-file $RuntimeEnvPath -p $projectName -f $composeBase -f $OverrideComposePath run --rm -T migrate alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "Database migration failed." }

    $claimArgs = @("run", "--rm", "-T", "migrate", "python", "-m", "app.cli_bootstrap", "ensure-setup-claim-token")
    if ($NewSetupCode) { $claimArgs += "--reissue" }
    if ($RecoverOwner) { $claimArgs += "--recover" }
    $claimJson = & docker compose --env-file $RuntimeEnvPath -p $projectName -f $composeBase -f $OverrideComposePath @claimArgs | Select-Object -Last 1
    if ($LASTEXITCODE -ne 0) { throw "Failed to evaluate installation claim token." }

    Write-Info "Starting backend API, worker, beat, frontend, and reverse proxy..."
    & docker compose --env-file $RuntimeEnvPath -p $projectName -f $composeBase -f $OverrideComposePath up -d --build
    if ($LASTEXITCODE -ne 0) { throw "Failed to start ApplyFlow containers." }

    $needsClaim = Show-ClaimOutput -JsonLine $claimJson -SetupUrl "$baseUrl/setup"
    Write-Info "Waiting for API health check at $baseUrl/api/v1/health..."
    $null = Wait-HttpHealthy -HealthUrl "$baseUrl/api/v1/health" -TimeoutSeconds 120
    $targetUrl = if ($needsClaim) { "$baseUrl/setup" } else { "$baseUrl/dashboard" }
    Open-BrowserUrl -Url $targetUrl
    exit 0
}

# =============================================================================
# MODE: LOCAL SINGLE-HOST (Python venv + Uvicorn + Vite, no Docker/Redis)
# =============================================================================
$venvPython = Join-Path $BackendDir ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    $sysPy = Get-Command python -ErrorAction SilentlyContinue
    if (-not $sysPy) {
        Write-Err "Python backend virtual environment ($venvPython) and system 'python' were not found."
        exit 1
    }
    $venvPython = $sysPy.Source
}
$npmCmd = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $npmCmd) { $npmCmd = Get-Command npm -ErrorAction SilentlyContinue }
if (-not $npmCmd) {
    Write-Err "Node.js / npm is required to start the frontend locally. Install Node.js 20+ or use Docker mode."
    exit 1
}
if (-not (Test-Path (Join-Path $FrontendDir "node_modules"))) {
    Write-Info "Installing frontend dependencies cleanly (first-time local setup)..."
    Push-Location $FrontendDir
    try {
        & $npmCmd.Source install --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw "npm install failed." }
    } finally {
        Pop-Location
    }
}

$localDbPath = Join-Path $DataDir "applyflow_local.db"
$dbExistsOnDisk = (Test-Path $localDbPath) -or (Test-Path (Join-Path $DataDir "applyflow_dev.db"))
$secretFiles = Ensure-BootstrapSecrets -DatabaseDataAlreadyExists $dbExistsOnDisk -ExistingEnvFile $existingEnvPath

Stop-LocalProcesses

$backendPort = if ($persisted -and $persisted.backend_port -and -not $ReconfigurePorts) {
    [int]$persisted.backend_port
} else {
    Find-FreePort -PreferredPorts @(8000, 8010, 8020, 8030)
}
$frontendPort = if ($persisted -and $persisted.frontend_port -and -not $ReconfigurePorts) {
    [int]$persisted.frontend_port
} else {
    Find-FreePort -PreferredPorts @(5173, 5174, 5175, 5180)
}

if (-not (Test-TcpPortFree -Port $backendPort)) {
    Write-Err "Persisted backend port $backendPort is busy; freeing it preserves OAuth redirect URLs. Free port $backendPort or rerun with -ReconfigurePorts."
    exit 1
}
if (-not (Test-TcpPortFree -Port $frontendPort)) {
    Write-Err "Persisted frontend port $frontendPort is busy. Free port $frontendPort or rerun with -ReconfigurePorts."
    exit 1
}

$localAppUrl = "http://localhost:$frontendPort"
$localApiUrl = "http://localhost:$backendPort"
$dbUrlPath = ($localDbPath -replace "\\", "/")

Save-PersistedMeta @{
    mode = "local"
    backend_port = $backendPort
    frontend_port = $frontendPort
    app_url = $localAppUrl
    api_public_url = $localApiUrl
    updated_at = (Get-Date).ToUniversalTime().ToString("o")
}

# Populate process-scoped non-secret config + file references (encryption keys stay out of .env files & args)
$env:ENVIRONMENT = "development"
$env:APP_URL = $localAppUrl
$env:API_PUBLIC_URL = $localApiUrl
$env:ALLOWED_ORIGINS = "$localAppUrl,http://127.0.0.1:$frontendPort"
$env:COOKIE_SECURE = "false"
$env:LIVE_SENDING_ENABLED = "false"
$env:DATABASE_URL = "sqlite:///$dbUrlPath"
$env:CELERY_TASK_ALWAYS_EAGER = "true"
$env:LOCAL_STORAGE_PATH = $StorageDir
$env:MAILBOX_ENCRYPTION_KEY_FILE = $secretFiles.EncryptionKeyFile
$env:APP_SECRET_KEY_FILE = $secretFiles.AppSecretFile
$env:VITE_API_PROXY_TARGET = "http://127.0.0.1:$backendPort"

Push-Location $BackendDir
try {
    Write-Info "Checking encryption key integrity..."
    & $venvPython -m app.cli_bootstrap check-encryption-integrity
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Encryption key integrity check failed. Refusing to overwrite existing keys."
        exit 2
    }

    Write-Info "Running database migrations..."
    & $venvPython -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "Alembic migration failed." }

    $claimArgs = @("-m", "app.cli_bootstrap", "ensure-setup-claim-token")
    if ($NewSetupCode) { $claimArgs += "--reissue" }
    if ($RecoverOwner) { $claimArgs += "--recover" }
    $claimJson = & $venvPython @claimArgs | Select-Object -Last 1
    if ($LASTEXITCODE -ne 0) { throw "Setup claim bootstrap failed." }
} finally {
    Pop-Location
}

$backendOut = Join-Path $LogsDir "local_backend.log"
$backendErr = Join-Path $LogsDir "local_backend_err.log"
$frontendOut = Join-Path $LogsDir "local_frontend.log"
$frontendErr = Join-Path $LogsDir "local_frontend_err.log"

Write-Info "Starting local FastAPI backend on $localApiUrl..."
$backendProc = Start-Process -FilePath $venvPython -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$backendPort") -WorkingDirectory $BackendDir -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr -PassThru -WindowStyle Hidden

Write-Info "Starting local Vite frontend on $localAppUrl..."
$frontendProc = Start-Process -FilePath $npmCmd.Source -ArgumentList @("run", "dev", "--", "--host", "127.0.0.1", "--port", "$frontendPort", "--strictPort") -WorkingDirectory $FrontendDir -RedirectStandardOutput $frontendOut -RedirectStandardError $frontendErr -PassThru -WindowStyle Hidden

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText(
    $LocalPidsPath,
    (@{ backend_pid = $backendProc.Id; frontend_pid = $frontendProc.Id; backend_port = $backendPort; frontend_port = $frontendPort } | ConvertTo-Json),
    $utf8NoBom
)

$needsClaim = Show-ClaimOutput -JsonLine $claimJson -SetupUrl "$localAppUrl/setup"

Write-Info "Waiting for local API and UI readiness..."
$healthy = Wait-HttpHealthy -HealthUrl "http://127.0.0.1:$backendPort/api/v1/health" -TimeoutSeconds 45
if (-not $healthy) {
    Write-Err "Backend did not become healthy within 45s. Check logs at $backendErr"
    exit 1
}

$targetUrl = if ($needsClaim) { "$localAppUrl/setup" } else { "$localAppUrl/dashboard" }
Write-Info "ApplyFlow Global local mode running (single-host SQLite + eager inline jobs; use Docker Desktop for PostgreSQL+Redis+Beat workers)."
Write-Info "To stop background processes later: .\Start-ApplyFlow.ps1 -Stop"
Open-BrowserUrl -Url $targetUrl
