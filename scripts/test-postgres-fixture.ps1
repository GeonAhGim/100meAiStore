$ErrorActionPreference = 'Stop'
# Only the already-present local image. No network, ports or host-data mounts.
$fixtureImage = 'postgres:16'
$fixtureName = 'store-contract-' + [guid]::NewGuid().ToString('N')
$fixtureLabel = 'com.100me.offline-fixture'
$fixtureSql = Join-Path $PSScriptRoot '../tests/contracts/postgres_rls_fixture.sql'
$fixtureId = $null
docker image inspect $fixtureImage --format '{{.Id}}'
if ($LASTEXITCODE -ne 0) { throw 'Existing postgres:16 image required; pulling is forbidden' }
try {
    $fixtureId = docker run --detach --pull never --rm --name $fixtureName --label "$fixtureLabel=$fixtureName" --network none --tmpfs /var/lib/postgresql/data:rw --env POSTGRES_HOST_AUTH_METHOD=trust $fixtureImage
    if ($LASTEXITCODE -ne 0 -or $fixtureId -notmatch '^[a-f0-9]{64}$') { throw 'Fixture start failed' }
    $fixtureReady = $false
    for ($fixtureAttempt = 0; $fixtureAttempt -lt 30; $fixtureAttempt++) {
        docker exec $fixtureId pg_isready -U postgres *> $null
        if ($LASTEXITCODE -eq 0) { $fixtureReady = $true; break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $fixtureReady) { throw 'Fixture database readiness timed out' }
    Get-Content -LiteralPath $fixtureSql -Raw | docker exec -i $fixtureId psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres
    if ($LASTEXITCODE -ne 0) { throw 'Fixture SQL failed' }
} finally {
    if ($fixtureId -and $fixtureId -match '^[a-f0-9]{64}$') {
        $fixtureIdentity = docker inspect --format "{{.Id}}|{{index .Config.Labels `"$fixtureLabel`"}}" $fixtureId
        if ($LASTEXITCODE -eq 0 -and $fixtureIdentity -eq "$fixtureId|$fixtureName") {
            docker stop --timeout 5 $fixtureId
            if ($LASTEXITCODE -ne 0) { throw "Owned fixture cleanup failed: $fixtureId" }
            $fixtureRemaining = docker ps -a --no-trunc --filter "id=$fixtureId" --format '{{.ID}}'
            if ($LASTEXITCODE -ne 0 -or $fixtureRemaining) { throw "Owned fixture removal not confirmed: $fixtureId" }
        } else { throw "Refusing cleanup: fixture ownership could not be verified for $fixtureId" }
    }
}
