-keep class com.yausername.youtubedl_android.** { *; }
# Commons Compress registra implementações ZIP por nome/reflexão. Obfuscar
# essas classes faz a release falhar em YoutubeDL.initPython(), embora a build
# debug funcione normalmente.
-keep class org.apache.commons.compress.** { *; }

# Os nomes JNI fazem parte do ABI entre Kotlin e libslucss_upscale.so.
-keep class com.xard.ytsystem.upscale.NativeUpscaler { *; }
-keep interface org.apache.commons.compress.** { *; }
-keep class net.i2p.crypto.eddsa.** { *; }
-keep class com.xard.ytsystem.api.** { *; }
# Mantém o fluxo de atualização transparente para auditorias do APK.
-keep class com.xard.ytsystem.update.** { *; }
-keepattributes *Annotation*
-dontwarn org.apache.commons.**
-dontwarn sun.security.x509.X509Key
