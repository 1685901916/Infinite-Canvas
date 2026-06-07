param(
  [switch]$OpenWhenReady
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (Test-Path -LiteralPath (Join-Path $ScriptDir "main.py")) {
  $AppDir = $ScriptDir
} else {
  $AppDir = Join-Path $ScriptDir "Infinite-Canvas-main"
}

$PythonExe = Join-Path $AppDir "python\python.exe"
$MainPy = Join-Path $AppDir "main.py"
$Url = "http://127.0.0.1:3001/"
$LogDir = Join-Path $AppDir "output"
$OutLog = Join-Path $LogDir "launcher-server.out.log"
$ErrLog = Join-Path $LogDir "launcher-server.err.log"

function Test-CanvasServer {
  try {
    return [bool](Get-NetTCPConnection -LocalPort 3001 -State Listen -ErrorAction SilentlyContinue)
  } catch {
    return $false
  }
}

if (Test-CanvasServer) {
  if ($OpenWhenReady) {
    Start-Process $Url | Out-Null
  }
  exit 0
}

if (-not (Test-Path -LiteralPath $MainPy)) {
  throw "Cannot find main.py: $MainPy"
}

if (-not (Test-Path -LiteralPath $PythonExe)) {
  $PythonExe = "python"
}

if (-not (Test-Path -LiteralPath $LogDir)) {
  New-Item -ItemType Directory -Path $LogDir | Out-Null
}

Start-Process -FilePath $PythonExe `
  -ArgumentList @("main.py") `
  -WorkingDirectory $AppDir `
  -WindowStyle Minimized `
  -RedirectStandardOutput $OutLog `
  -RedirectStandardError $ErrLog | Out-Null

if ($OpenWhenReady) {
  $deadline = (Get-Date).AddSeconds(60)
  while ((Get-Date) -lt $deadline) {
    if (Test-CanvasServer) {
      Start-Process $Url | Out-Null
      exit 0
    }
    Start-Sleep -Milliseconds 700
  }
  exit 2
}

exit 0
