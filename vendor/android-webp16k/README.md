# WebP ARM64 — 16 KB

Libwebp v1.6.0, upstream commit `4fa21912338357f89e4fd51cf2368325b59e9bd9`.
Source: https://chromium.googlesource.com/webm/libwebp

Built with Android NDK 29.0.14206865, API 26, CMake 3.22.1, Ninja,
`ANDROID_ABI=arm64-v8a`, `BUILD_SHARED_LIBS=ON`, `CMAKE_BUILD_TYPE=Release`.
Command-line utilities disabled; shared encoder, decoder, demux, mux and
sharpyuv enabled. Stripped with NDK `llvm-strip --strip-unneeded`.

These five libraries replace the corresponding files inside FFmpeg 0.18.1's
ARM64 archive at build time. The original AAR is SHA-256 pinned in Gradle;
replacement hashes are in SHA256SUMS.txt. Original upstream exported symbols
are checked against replacements during preparation. No download occurs on the
phone. COPYING and PATENTS retain upstream license terms.

Run tools/audit_mobile_apk.py on the final APK to inspect **nested** native
libraries. Physical ARM64 / 16 KB execution remains a separate required check.
