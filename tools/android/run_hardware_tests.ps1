# Thin launcher; all device selection, verification and reports live in Python.
$ErrorActionPreference = 'Stop'
$LastphoneRunner = Join-Path $PSScriptRoot 'run_hardware_tests.py'
$LastphoneCandidates = @(
    @{ Name = 'py'; Prefix = @('-3') },
    @{ Name = 'python3'; Prefix = @() },
    @{ Name = 'python'; Prefix = @() }
)
foreach ($LastphoneCandidate in $LastphoneCandidates) {
    $LastphoneCommand = Get-Command $LastphoneCandidate.Name -CommandType Application -ErrorAction SilentlyContinue
    if ($null -eq $LastphoneCommand) { continue }
    $LastphoneExecutable = $LastphoneCommand.Source
    $LastphonePrefix = $LastphoneCandidate.Prefix
    & $LastphoneExecutable @LastphonePrefix -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'
    if ($LASTEXITCODE -ne 0) { continue }
    & $LastphoneExecutable @LastphonePrefix $LastphoneRunner @args
    exit $LASTEXITCODE
}
throw 'Python 3.10 or newer is required. Install Python, or run run_hardware_tests.py with your existing interpreter.'
