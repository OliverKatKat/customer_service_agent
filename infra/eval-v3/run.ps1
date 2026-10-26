<#
.SYNOPSIS
Runs one native command with validated eval v3 service URLs.
.DESCRIPTION
The app still reads app/.env for unrelated secrets. This wrapper overrides all
data-store URLs in the child process and restores the caller's environment.
#>
[CmdletBinding()]
param(
    [string]$EnvFile = (Join-Path $PSScriptRoot '.env'),
    [Parameter(Mandatory = $true)][string]$Exe,
    [string[]]$Arguments = @()
)

$ErrorActionPreference = 'Stop'
$resolvedFile = (Resolve-Path -LiteralPath $EnvFile -ErrorAction Stop).Path
$values = @{}
foreach ($line in [System.IO.File]::ReadAllLines($resolvedFile)) {
    $entry = $line.Trim()
    if (-not $entry -or $entry.StartsWith('#')) { continue }
    $separator = $entry.IndexOf('=')
    if ($separator -le 0) { throw "Invalid eval env line: $entry" }
    $key = $entry.Substring(0, $separator).Trim()
    $value = $entry.Substring($separator + 1).Trim()
    if ($value.Length -ge 2 -and $value[0] -eq '"' -and $value[-1] -eq '"') {
        $value = $value.Substring(1, $value.Length - 2)
    }
    $values[$key] = $value
}

$required = @(
    'EVAL_V3_DB_PASSWORD', 'EVAL_V3_DATABASE_URL',
    'EVAL_V3_REDIS_URL', 'EVAL_V3_QDRANT_URL',
    'EVAL_V3_COMPANY_KNOWLEDGE_COLLECTION', 'EVAL_V3_PASSWORD',
    'EVAL_V3_APPROVAL_CODE'
)
foreach ($key in $required) {
    if (-not $values.ContainsKey($key) -or -not $values[$key]) {
        throw "Missing $key in $resolvedFile"
    }
}

if ($values['EVAL_V3_PASSWORD'].Length -lt 12) { throw 'EVAL_V3_PASSWORD must be at least 12 characters.' }
if ($values['EVAL_V3_APPROVAL_CODE'].Length -lt 16) { throw 'EVAL_V3_APPROVAL_CODE must be at least 16 characters.' }

$db = [Uri]$values['EVAL_V3_DATABASE_URL']
$redis = [Uri]$values['EVAL_V3_REDIS_URL']
$qdrant = [Uri]$values['EVAL_V3_QDRANT_URL']
if ($db.Scheme -ne 'postgresql+asyncpg' -or $db.Host -ne '127.0.0.1' -or
    $db.Port -ne 55432 -or $db.AbsolutePath -ne '/agent_eval_v3' -or
    -not $db.UserInfo.StartsWith('eval_v3:') -or $db.Query -or $db.Fragment) {
    throw 'EVAL_V3_DATABASE_URL must target eval_v3@127.0.0.1:55432/agent_eval_v3 via asyncpg.'
}
if ($redis.Scheme -ne 'redis' -or $redis.Host -ne '127.0.0.1' -or
    $redis.Port -ne 56379 -or $redis.AbsolutePath -ne '/0' -or
    $redis.Query -or $redis.Fragment) {
    throw 'EVAL_V3_REDIS_URL must target redis://127.0.0.1:56379/0.'
}
if ($qdrant.Scheme -ne 'http' -or $qdrant.Host -ne '127.0.0.1' -or
    $qdrant.Port -ne 56333 -or $qdrant.AbsolutePath -ne '/' -or
    $qdrant.Query -or $qdrant.Fragment) {
    throw 'EVAL_V3_QDRANT_URL must target http://127.0.0.1:56333.'
}
if ($values['EVAL_V3_COMPANY_KNOWLEDGE_COLLECTION'] -ne 'company_knowledge_eval_v3') {
    throw 'The eval v3 company knowledge collection must be company_knowledge_eval_v3.'
}

$overrides = @{
    PYTHONIOENCODING = 'utf-8'
    PYTHONUTF8 = '1'
    EVAL_V3_DATABASE_URL = $values['EVAL_V3_DATABASE_URL']
    EVAL_V3_PASSWORD = $values['EVAL_V3_PASSWORD']
    ADMIN_APPROVAL_CODE = $values['EVAL_V3_APPROVAL_CODE']
    DATABASE_URL = $values['EVAL_V3_DATABASE_URL']
    READONLY_DATABASE_URL = $values['EVAL_V3_DATABASE_URL']
    REDIS_URL = $values['EVAL_V3_REDIS_URL']
    QDRANT_URL = $values['EVAL_V3_QDRANT_URL']
    COMPANY_KNOWLEDGE_COLLECTION = $values['EVAL_V3_COMPANY_KNOWLEDGE_COLLECTION']
}
$prior = @{}
foreach ($key in $overrides.Keys) {
    $prior[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
}
try {
    foreach ($key in $overrides.Keys) {
        [Environment]::SetEnvironmentVariable($key, $overrides[$key], 'Process')
    }
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Exe exited with code $LASTEXITCODE"
    }
}
finally {
    foreach ($key in $prior.Keys) {
        [Environment]::SetEnvironmentVariable($key, $prior[$key], 'Process')
    }
}
