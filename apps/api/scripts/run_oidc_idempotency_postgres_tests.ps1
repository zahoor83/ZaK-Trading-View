$ErrorActionPreference = "Stop"

$apiRoot = "D:\ZaK Trading View\apps\api"
$python = "C:\Users\zakal\AppData\Local\Programs\Python\Python313\python.exe"
$pgBin = "C:\Program Files\PostgreSQL\18\bin"
$passwordFile = Join-Path $env:LOCALAPPDATA "ZaKTradingView\PostgresTest\app-password.dpapi"

$oldDatabaseUrl = [Environment]::GetEnvironmentVariable("DATABASE_URL", "Process")
$oldPgPassword = [Environment]::GetEnvironmentVariable("PGPASSWORD", "Process")

$securePassword = $null
$bstr = [IntPtr]::Zero
$password = $null

try {
    Set-Location $apiRoot

    if (-not (Test-Path -LiteralPath $passwordFile -PathType Leaf)) {
        throw "DPAPI password file missing."
    }

    & (Join-Path $pgBin "pg_isready.exe") `
        -h 127.0.0.1 -p 55432 `
        -d zak_oidc_test -U zak_oidc_test_app

    if ($LASTEXITCODE -ne 0) {
        throw "Isolated PostgreSQL is not accepting connections."
    }

    $encrypted = (Get-Content -LiteralPath $passwordFile -Raw).Trim()
    $securePassword = ConvertTo-SecureString -String $encrypted

    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR(
        $securePassword
    )

    $password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)

    if ([string]::IsNullOrWhiteSpace($password)) {
        throw "Could not decrypt test database password."
    }

    [Environment]::SetEnvironmentVariable("PGPASSWORD", $password, "Process")

    $identity = & (Join-Path $pgBin "psql.exe") `
        -X -v ON_ERROR_STOP=1 `
        -h 127.0.0.1 -p 55432 `
        -U zak_oidc_test_app -d zak_oidc_test -At `
        -c "SELECT current_user || '|' || current_database() || '|' || current_setting('port') || '|' || current_setting('server_version_num');"

    if ($LASTEXITCODE -ne 0) {
        throw "Database identity verification failed."
    }

    if ($identity -notmatch '^zak_oidc_test_app\|zak_oidc_test\|55432\|180006$') {
        throw "Unexpected database identity or version. Refusing to continue."
    }

    Write-Host "Verified target: $identity" -ForegroundColor Green

    $encodedPassword = [Uri]::EscapeDataString($password)

    $databaseUrl = "postgresql+asyncpg://zak_oidc_test_app:$encodedPassword@127.0.0.1:55432/zak_oidc_test"

    [Environment]::SetEnvironmentVariable(
        "DATABASE_URL", $databaseUrl, "Process"
    )

    Write-Host "`nChecking Python dependencies..." -ForegroundColor Cyan

    & $python -c "import sqlalchemy, asyncpg, pytest, pytest_asyncio; print('SQLAlchemy:', sqlalchemy.__version__); print('asyncpg:', asyncpg.__version__); print('pytest:', pytest.__version__); print('pytest-asyncio: available')"

    if ($LASTEXITCODE -ne 0) {
        throw "Required dependencies are unavailable."
    }

    Write-Host "`nRunning PostgreSQL integration tests..." -ForegroundColor Cyan

    & $python -m pytest -q `
    tests/test_oidc_idempotency_postgres.py `
    tests/test_account_provisioning_postgres.py `
    tests/test_hmac_internal_service_auth_postgres.py `
    tests/test_hmac_cross_stack_https_postgres.py

    if ($LASTEXITCODE -ne 0) {
        throw "PostgreSQL integration tests failed."
    }

    Write-Host "`nPOSTGRESQL INTEGRATION TESTS PASSED" -ForegroundColor Green
}
finally {
    [Environment]::SetEnvironmentVariable(
        "DATABASE_URL", $oldDatabaseUrl, "Process"
    )

    [Environment]::SetEnvironmentVariable(
        "PGPASSWORD", $oldPgPassword, "Process"
    )

    if ($bstr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }

    $password = $null
    $securePassword = $null
    $databaseUrl = $null
    $encodedPassword = $null

    Write-Host "Process environment restored." -ForegroundColor Green
}

