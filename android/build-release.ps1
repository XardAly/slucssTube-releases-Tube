param([switch] $Offline)

$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$signingDir = Join-Path $env:APPDATA "SLUCSS YT SYSTEM\android-signing"
$keystore = Join-Path $signingDir "slucss-system-release.jks"
$secretFile = Join-Path $signingDir "signing-password.dpapi"
$javaHome = if ($env:JAVA_HOME) {
    $env:JAVA_HOME
} else {
    Join-Path $env:LOCALAPPDATA "SlucssAndroidToolchain\temurin17\jdk-17.0.20+8"
}
$androidHome = if ($env:ANDROID_HOME) {
    $env:ANDROID_HOME
} else {
    Join-Path $env:LOCALAPPDATA "Android\Sdk"
}

if (-not (Test-Path $keystore) -or -not (Test-Path $secretFile)) {
    throw "Chave de release ausente. Execute .\create-signing-key.ps1 uma única vez."
}
if (-not (Test-Path (Join-Path $javaHome "bin\java.exe"))) {
    throw "JDK 17 não encontrado."
}
if (-not (Test-Path (Join-Path $androidHome "platforms\android-35\android.jar"))) {
    throw "Android SDK 35 não encontrado."
}

$protectedPassword = (Get-Content -Raw -LiteralPath $secretFile).Trim()
$secure = $protectedPassword | ConvertTo-SecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
$password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
try {
    $env:JAVA_HOME = $javaHome
    $env:ANDROID_HOME = $androidHome
    $env:SLUCSS_ANDROID_KEYSTORE = $keystore
    $env:SLUCSS_ANDROID_STORE_PASSWORD = $password
    $env:SLUCSS_ANDROID_KEY_ALIAS = "slucss-system"
    $env:SLUCSS_ANDROID_KEY_PASSWORD = $password
    $gradleArguments = @("--no-daemon", "--console=plain")
    if ($Offline) { $gradleArguments += "--offline" }
    $gradleArguments += @(
        ":app:testDebugUnitTest", ":bootstrap:testDebugUnitTest",
        ":app:lintVitalRelease", ":bootstrap:lintRelease",
        ":app:assembleRelease", ":bootstrap:assembleRelease"
    )
    Push-Location $projectDir
    try {
        # Windows PowerShell wraps stderr warnings as NativeCommandError when
        # redirected. Only Gradle's exit code determines build failure.
        $ErrorActionPreference = "Continue"
        & (Join-Path $projectDir "gradlew.bat") @gradleArguments 2>&1 |
            ForEach-Object { Write-Output $_.ToString() }
        $gradleExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = "Stop"
        Pop-Location
    }
    if ($gradleExitCode -ne 0) { throw "A compilação da release falhou (código $gradleExitCode)." }
    $releaseDir = Join-Path $projectDir "app\build\outputs\apk\release"
    $dist = Join-Path $projectDir "dist"
    New-Item -ItemType Directory -Force -Path $dist | Out-Null
    # Universal + uma fatia por arquitetura: o backend entrega a fatia certa a
    # cada aparelho e o universal atende clientes antigos e download manual.
    $nomes = @("Slucss-System.apk", "Slucss-System-arm64-v8a.apk", "Slucss-System-armeabi-v7a.apk")
    foreach ($nome in $nomes) {
        $source = Join-Path $releaseDir $nome
        if (-not (Test-Path $source)) { throw "APK ausente na saida da compilacao: $nome" }
        Copy-Item -LiteralPath $source -Destination (Join-Path $dist $nome) -Force
        $tamanho = [math]::Round((Get-Item $source).Length / 1MB, 1)
        Write-Host "APK gerado: $nome ($tamanho MB)"
    }

    $installerName = "Slucss-System-Installer-1.0.2.apk"
    $installerSource = Join-Path $projectDir "bootstrap\build\outputs\apk\release\$installerName"
    if (-not (Test-Path $installerSource)) {
        throw "APK ausente na saida da compilacao: $installerName"
    }
    Copy-Item -LiteralPath $installerSource -Destination (Join-Path $dist $installerName) -Force
    $installerSizeKb = [math]::Round((Get-Item $installerSource).Length / 1KB)
    Write-Host "APK gerado: $installerName ($installerSizeKb KB)"

    $apksigner = Join-Path $androidHome "build-tools\35.0.0\apksigner.bat"
    if (-not (Test-Path $apksigner)) { throw "apksigner do Android 35 nao encontrado." }
    $aapt = Join-Path $androidHome "build-tools\35.0.0\aapt2.exe"
    if (-not (Test-Path $aapt)) { throw "aapt2 do Android 35 nao encontrado." }
    function Get-ApkCertificateSha256([string] $apkPath) {
        $certificateOutput = & $apksigner verify --print-certs $apkPath
        if ($LASTEXITCODE -ne 0) { throw "Assinatura APK invalida: $apkPath" }
        $certificateLines = @($certificateOutput |
            Select-String -Pattern 'certificate SHA-256 digest:\s*([0-9a-fA-F]{64})')
        if ($certificateLines.Count -ne 1) {
            throw "O APK precisa ter exatamente um assinante: $apkPath"
        }
        return $certificateLines[0].Matches[0].Groups[1].Value.ToLowerInvariant()
    }
    $expectedCertificate = "2475687690fc94a3d27ab6206f804f963a91ebb781d96ca67a223f805dd8a775"
    $appPath = Join-Path $dist "Slucss-System.apk"
    $installerPath = Join-Path $dist $installerName
    $installerCertificate = Get-ApkCertificateSha256 $installerPath
    $appCertificatesValid = $true
    foreach ($nome in $nomes) {
        if ((Get-ApkCertificateSha256 (Join-Path $dist $nome)) -ne $expectedCertificate) {
            $appCertificatesValid = $false
        }
    }
    if (-not $appCertificatesValid -or $installerCertificate -ne $expectedCertificate) {
        throw "O app e o instalador precisam usar o certificado oficial e a mesma chave de release."
    }

    function Get-ApkBadging([string] $apkPath) {
        $badging = & $aapt dump badging $apkPath
        if ($LASTEXITCODE -ne 0) { throw "Nao foi possivel ler os metadados do APK: $apkPath" }
        $packageLine = $badging | Select-String -Pattern "^package: name='([^']+)' versionCode='([0-9]+)'" | Select-Object -First 1
        if (-not $packageLine) { throw "Pacote/versionCode ausente no APK: $apkPath" }
        return @{
            PackageName = $packageLine.Matches[0].Groups[1].Value
            VersionCode = [int]$packageLine.Matches[0].Groups[2].Value
        }
    }
    $appBadging = Get-ApkBadging $appPath
    $installerBadging = Get-ApkBadging $installerPath
    if ($installerBadging.PackageName -ne "com.xard.ytsystem" -or
        $appBadging.PackageName -ne $installerBadging.PackageName -or
        $installerBadging.VersionCode -ge $appBadging.VersionCode) {
        throw "O instalador precisa ter o mesmo package e versionCode menor que o app completo."
    }
    foreach ($nome in $nomes) {
        $sliceBadging = Get-ApkBadging (Join-Path $dist $nome)
        if ($sliceBadging.PackageName -ne $appBadging.PackageName -or
            $sliceBadging.VersionCode -ne $appBadging.VersionCode) {
            throw "As variantes do app precisam ter o mesmo package e versionCode: $nome"
        }
    }
    $installerPermissions = @(& $aapt dump permissions $installerPath |
        Select-String -Pattern "^uses-permission: name='([^']+)'" |
        ForEach-Object { $_.Matches[0].Groups[1].Value } |
        Sort-Object)
    $expectedInstallerPermissions = @(
        "android.permission.INTERNET",
        "android.permission.REQUEST_INSTALL_PACKAGES",
        "com.xard.ytsystem.DYNAMIC_RECEIVER_NOT_EXPORTED_PERMISSION"
    ) | Sort-Object
    if (@(Compare-Object $installerPermissions $expectedInstallerPermissions).Count -ne 0) {
        throw "O instalador leve possui um conjunto inesperado de permissoes."
    }
    Write-Host "Certificado conferido: app e instalador usam a mesma chave oficial."
    Write-Host "Fluxo de atualizacao conferido: mesmo pacote, versionCode menor e permissoes esperadas."
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    $password = $null
    Remove-Item Env:SLUCSS_ANDROID_KEYSTORE -ErrorAction SilentlyContinue
    Remove-Item Env:SLUCSS_ANDROID_STORE_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:SLUCSS_ANDROID_KEY_ALIAS -ErrorAction SilentlyContinue
    Remove-Item Env:SLUCSS_ANDROID_KEY_PASSWORD -ErrorAction SilentlyContinue
}
