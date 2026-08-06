# JARVIS setup. Run this deliberately — it installs things.
#
#   powershell -ExecutionPolicy Bypass -File jarvis\setup.ps1
#
# Creates a venv at jarvis\.venv, installs only the packages the build spec named,
# writes a .env if there isn't one, and checks both keys actually work. Nothing
# here touches anything outside the jarvis folder.

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $here ".venv"
$py   = Join-Path $venv "Scripts\python.exe"

Write-Host ""
Write-Host "  J A R V I S   setup" -ForegroundColor Cyan
Write-Host ""

# --- python -------------------------------------------------------------------
$sys = Get-Command python -ErrorAction SilentlyContinue
if (-not $sys) { Write-Host "  python is not on PATH. Install Python 3.10+ first." -ForegroundColor Red; exit 1 }
$ver = & python -c "import sys;print('%d.%d'%sys.version_info[:2])"
Write-Host "  python $ver"
if ([version]$ver -lt [version]"3.10") {
  Write-Host "  JARVIS needs Python 3.10 or newer." -ForegroundColor Red; exit 1
}

# --- venv ---------------------------------------------------------------------
if (-not (Test-Path $py)) {
  Write-Host "  creating venv at $venv"
  & python -m venv $venv
} else {
  Write-Host "  venv already exists"
}

# --- packages -----------------------------------------------------------------
# Exactly the set the build spec named and expected. Nothing else is installed
# without asking. Total download is roughly 300-400 MB, most of it the wake-word
# model's runtime — expect it to take a few minutes the first time.
$pkgs = @(
  "requests",       # HTTP where the stdlib gets awkward
  "numpy",          # audio maths
  "sounddevice",    # microphone in and speaker out, 16 kHz mono
  "openwakeword",   # "Hey Jarvis", running locally, always on
  "uiautomation",   # Windows UI Automation - focus, read window trees, click by name
  "pywin32",        # window handles and the global hotkey
  "pyautogui",      # LAST-RESORT coordinate clicking only, and it announces itself
  "playwright"      # step 5, real Chrome over CDP
)
Write-Host ""
Write-Host "  installing:" -ForegroundColor Cyan
$pkgs | ForEach-Object { Write-Host "    $_" }
Write-Host ""
& $py -m pip install --upgrade pip --quiet
& $py -m pip install --quiet @pkgs
if ($LASTEXITCODE -ne 0) { Write-Host "  pip failed - see above." -ForegroundColor Red; exit 1 }
Write-Host "  packages installed" -ForegroundColor Cyan

# --- .env ---------------------------------------------------------------------
$envFile = Join-Path $here ".env"
if (-not (Test-Path $envFile)) {
  Copy-Item (Join-Path $here ".env.example") $envFile
  Write-Host ""
  Write-Host "  created jarvis\.env - open it and paste your keys in, then run this again." -ForegroundColor Yellow
}

# Restrict .env to the current user. It holds live API keys.
try {
  $acl = Get-Acl $envFile
  $acl.SetAccessRuleProtection($true, $false)
  $acl.Access | ForEach-Object { $acl.RemoveAccessRule($_) | Out-Null }
  $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
    "$env:USERNAME", "FullControl", "Allow")
  $acl.SetAccessRule($rule)
  Set-Acl $envFile $acl
  Write-Host "  .env locked to $env:USERNAME"
} catch {
  Write-Host "  could not restrict .env permissions - check them yourself." -ForegroundColor Yellow
}

# --- key checks ---------------------------------------------------------------
# Both keys are verified against the live APIs. A key that is present but wrong is
# worse than one that is missing: it fails later, in the middle of a turn.
Write-Host ""
Write-Host "  checking keys" -ForegroundColor Cyan
& $py -c @"
import os, sys, json, urllib.request, urllib.error
sys.path.insert(0, r'$here')
from agent.runtime import load_dotenv
load_dotenv()

def check(name, url, headers):
    key = os.environ.get(name, '').strip()
    if not key:
        print(f'    {name:22s} NOT SET  - add it to jarvis\\.env')
        return
    try:
        req = urllib.request.Request(url, headers=headers(key))
        with urllib.request.urlopen(req, timeout=20) as r:
            json.loads(r.read())
        print(f'    {name:22s} OK')
    except urllib.error.HTTPError as e:
        print(f'    {name:22s} REJECTED (HTTP {e.code}) - the key is present but not valid')
    except Exception as e:
        print(f'    {name:22s} UNREACHABLE - {type(e).__name__}')

check('GEMINI_API_KEY',
      'https://generativelanguage.googleapis.com/v1beta/models',
      lambda k: {'x-goog-api-key': k})
check('ELEVENLABS_API_KEY',
      'https://api.elevenlabs.io/v1/user',
      lambda k: {'xi-api-key': k})
"@

# --- microphones --------------------------------------------------------------
# Enumerated at startup and chosen by NAME, because indices shift the moment a
# USB headset appears and JARVIS must not go silently deaf.
Write-Host ""
Write-Host "  input devices" -ForegroundColor Cyan
& $py -c @"
import sys
sys.path.insert(0, r'$here')
try:
    from agent.voice import list_devices
    for d in list_devices():
        print(f\"    [{d['index']}] {d['name']}\")
    print()
    print('    Put the NAME of the one you want in .env as JARVIS_MIC_NAME.')
except Exception as e:
    print(f'    could not enumerate: {e}')
"@

Write-Host ""
Write-Host "  done." -ForegroundColor Cyan
Write-Host ""
Write-Host "  next:"
Write-Host "    .venv\Scripts\python jarvis\server.py     the HUD, at http://127.0.0.1:8765"
Write-Host "    .venv\Scripts\python jarvis\run.py        the text box"
Write-Host "    .venv\Scripts\python jarvis\prove.py      watch the guardrails refuse things"
Write-Host ""
Write-Host "  JARVIS_DRYRUN=1 is set by default. Nothing it does is real until you" -ForegroundColor Yellow
Write-Host "  change that in .env, deliberately." -ForegroundColor Yellow
Write-Host ""
