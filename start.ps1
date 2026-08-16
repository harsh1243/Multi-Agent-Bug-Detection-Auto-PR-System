$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if (-not $launcher) {
        $launcher = Get-Command python -ErrorAction SilentlyContinue
    }
    if (-not $launcher) {
        throw "Python 3.11+ is required. Install Python, then run this script again."
    }
    & $launcher.Source -m venv .venv
}

& $venvPython -m pip install --quiet --requirement backend\requirements.txt
& $venvPython -m streamlit run streamlit_app.py
