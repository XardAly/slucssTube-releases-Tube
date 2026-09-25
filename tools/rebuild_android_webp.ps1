$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$sdk = if ($env:ANDROID_HOME) { $env:ANDROID_HOME } else { Join-Path $env:LOCALAPPDATA "Android\Sdk" }
$ndk = Join-Path $sdk "ndk\29.0.14206865"
$cmake = Join-Path $sdk "cmake\3.22.1\bin\cmake.exe"
$ninja = Join-Path $sdk "cmake\3.22.1\bin\ninja.exe"
$source = Join-Path $root ".devdownloads\mobile-v1-libwebp"
$build = Join-Path $root ".devdownloads\mobile-v1-webp-arm64"
$output = Join-Path $root "vendor\android-webp16k"
$commit = "4fa21912338357f89e4fd51cf2368325b59e9bd9"
if (-not (Test-Path -LiteralPath $source)) {
    & git clone --depth 1 --branch v1.6.0 https://chromium.googlesource.com/webm/libwebp $source
    if ($LASTEXITCODE -ne 0) { throw "WebP source download failed" }
}
if ((& git -C $source rev-parse HEAD) -ne $commit) { throw "Unexpected WebP source commit" }
& $cmake -S $source -B $build -G Ninja "-DCMAKE_MAKE_PROGRAM=$ninja" "-DCMAKE_TOOLCHAIN_FILE=$ndk/build/cmake/android.toolchain.cmake" -DANDROID_ABI=arm64-v8a -DANDROID_PLATFORM=android-26 -DBUILD_SHARED_LIBS=ON -DCMAKE_BUILD_TYPE=Release -DWEBP_BUILD_ANIM_UTILS=OFF -DWEBP_BUILD_CWEBP=OFF -DWEBP_BUILD_DWEBP=OFF -DWEBP_BUILD_GIF2WEBP=OFF -DWEBP_BUILD_IMG2WEBP=OFF -DWEBP_BUILD_VWEBP=OFF -DWEBP_BUILD_WEBPINFO=OFF -DWEBP_BUILD_WEBPMUX=OFF -DWEBP_BUILD_EXTRAS=OFF
if ($LASTEXITCODE -ne 0) { throw "WebP configuration failed" }
& $cmake --build $build -j 4
if ($LASTEXITCODE -ne 0) { throw "WebP build failed" }
New-Item -ItemType Directory -Force -Path $output | Out-Null
$hashes = @()
foreach ($name in @("libsharpyuv.so", "libwebp.so", "libwebpdecoder.so", "libwebpdemux.so", "libwebpmux.so")) {
    $destination = Join-Path $output $name
    Copy-Item -LiteralPath (Join-Path $build $name) -Destination $destination
    & "$ndk/toolchains/llvm/prebuilt/windows-x86_64/bin/llvm-strip.exe" --strip-unneeded $destination
    if ($LASTEXITCODE -ne 0) { throw "WebP strip failed" }
    $hashes += (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant() + "  " + $name
}
[IO.File]::WriteAllLines((Join-Path $output "SHA256SUMS.txt"), $hashes, [Text.UTF8Encoding]::new($false))
Write-Output "WebP rebuilt. Review exported symbols and run tools/audit_mobile_apk.py after building the APK."
