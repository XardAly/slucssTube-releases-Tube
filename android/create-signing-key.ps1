$ErrorActionPreference = "Stop"

$signingDir = Join-Path $env:APPDATA "SLUCSS YT SYSTEM\android-signing"
$keystore = Join-Path $signingDir "slucss-system-release.jks"
$secretFile = Join-Path $signingDir "signing-password.dpapi"
$javaHome = if ($env:JAVA_HOME) {
    $env:JAVA_HOME
} else {
    Join-Path $env:LOCALAPPDATA "SlucssAndroidToolchain\temurin17\jdk-17.0.20+8"
}
$keytool = Join-Path $javaHome "bin\keytool.exe"

if (-not (Test-Path $keytool)) {
    throw "JDK 17 não encontrado. Configure JAVA_HOME antes de criar a chave."
}
if ((Test-Path $keystore) -or (Test-Path $secretFile)) {
    throw "A chave Android já existe. Nada foi sobrescrito."
}

New-Item -ItemType Directory -Force -Path $signingDir | Out-Null
$random = [byte[]]::new(36)
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
$rng.GetBytes($random)
$rng.Dispose()
$password = [Convert]::ToBase64String($random).TrimEnd('=').Replace('+', '-').Replace('/', '_')
$secure = ConvertTo-SecureString $password -AsPlainText -Force
$secure | ConvertFrom-SecureString | Set-Content -LiteralPath $secretFile -Encoding UTF8

try {
    & $keytool -genkeypair `
        -keystore $keystore `
        -storetype PKCS12 `
        -storepass $password `
        -keypass $password `
        -alias "slucss-system" `
        -keyalg RSA `
        -keysize 4096 `
        -validity 10000 `
        -dname "CN=Slucss System, OU=Release, O=Slucss, C=BR"
    if ($LASTEXITCODE -ne 0) { throw "O keytool não conseguiu criar a chave." }
} catch {
    Remove-Item -LiteralPath $secretFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $keystore -Force -ErrorAction SilentlyContinue
    throw
} finally {
    $password = $null
    [Array]::Clear($random, 0, $random.Length)
}

Write-Host "Chave Android criada fora do repositório."
Write-Host "Faça backup seguro de: $keystore"
Write-Host "A senha está protegida por DPAPI para este usuário do Windows."
