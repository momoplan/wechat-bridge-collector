param(
  [string]$Config = "",
  [string]$HealthUrl = "http://127.0.0.1:18082/health"
)

$ErrorActionPreference = "Stop"
$LogDir = Join-Path "{{STATE_DIR}}" "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

try {
  $resp = Invoke-WebRequest -UseBasicParsing -Uri $HealthUrl -TimeoutSec 2
  if ($resp.StatusCode -eq 200) {
    Write-Output "collector already healthy: $HealthUrl"
    exit 0
  }
} catch {
}

$Python = "{{PYTHON}}"
if (-not (Test-Path $Python)) {
  $Python = (Get-Command python -ErrorAction Stop).Source
}

$out = Join-Path $LogDir "collector.out.log"
$err = Join-Path $LogDir "collector.err.log"
$args = @("-u", "-m", "wechat_bridge_collector")
if ($Config) {
  $args += @("--config", $Config)
}
$args += @("run")

Start-Process -FilePath $Python `
  -ArgumentList $args `
  -WindowStyle Hidden `
  -RedirectStandardOutput $out `
  -RedirectStandardError $err

Start-Sleep -Seconds 5
try {
  $resp = Invoke-WebRequest -UseBasicParsing -Uri $HealthUrl -TimeoutSec 2
  if ($resp.StatusCode -eq 200) {
    Write-Output "collector started: $HealthUrl"
    exit 0
  }
} catch {
}

Write-Output "collector start command issued; health not ready yet: $HealthUrl"
exit 0
