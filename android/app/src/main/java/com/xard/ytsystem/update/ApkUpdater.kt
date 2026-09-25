package com.xard.ytsystem.update

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.core.content.FileProvider
import com.xard.ytsystem.BuildConfig
import com.xard.ytsystem.api.AndroidManifest
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import java.io.File
import java.io.FileOutputStream
import java.nio.file.AtomicMoveNotSupportedException
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.TimeUnit

class ApkUpdater(private val activity: Activity) {
    private val http = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .callTimeout(10, TimeUnit.MINUTES)
        .followSslRedirects(false)
        .build()

    fun needsUnknownSourcesPermission(): Boolean =
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.O &&
            !activity.packageManager.canRequestPackageInstalls()

    fun requestUnknownSourcesPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            activity.startActivity(
                Intent(
                    Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:${activity.packageName}"),
                ),
            )
        }
    }

    suspend fun download(
        manifest: AndroidManifest,
        onProgress: (Int) -> Unit,
    ): File = withContext(Dispatchers.IO) {
        require(manifest.sha256.matches(Regex("[0-9a-f]{64}"))) {
            "A atualização ainda não foi publicada com hash de segurança."
        }
        require(manifest.latestVersionCode > 0) { "A versão publicada é inválida." }
        require(isOfficialDownloadUrl(manifest.downloadUrl)) {
            "A atualização não veio do repositório oficial."
        }
        val directory = File(activity.cacheDir, "updates").apply { mkdirs() }
        val partial = File(directory, "Slucss-System-${UUID.randomUUID()}.apk.part")
        val output = File(directory, "Slucss-System.apk")
        val request = Request.Builder()
            .url(manifest.downloadUrl)
            .header("User-Agent", "Slucss-Android/${BuildConfig.VERSION_NAME}")
            .get()
            .build()
        val call = http.newCall(request)
        val activeJob = currentCoroutineContext()[Job]
        val cancellation = activeJob?.invokeOnCompletion { cause ->
            if (cause is CancellationException) call.cancel()
        }
        try {
            call.execute().use { response ->
                if (!response.isSuccessful) error("O servidor não liberou o APK (${response.code}).")
                if (response.request.url.scheme != "https") {
                    throw SecurityException("O download saiu da conexão segura.")
                }
                val body = response.body ?: error("O servidor retornou um arquivo vazio.")
                val expected = body.contentLength()
                if (expected > MAX_APK_BYTES) error("O APK excede o limite de segurança.")
                val digest = MessageDigest.getInstance("SHA-256")
                var total = 0L
                // O callback ia a cada bloco de 256 KB — ~380 vezes num APK de
                // 95 MB — e cada uma reconstruía o diálogo na thread principal.
                // Só avisa quando o número exibido muda de fato.
                var lastPercent = -1
                body.byteStream().use { input ->
                    FileOutputStream(partial).use { file ->
                        val buffer = ByteArray(256 * 1024)
                        while (true) {
                            activeJob?.ensureActive()
                            val read = input.read(buffer)
                            if (read < 0) break
                            total += read
                            if (total > MAX_APK_BYTES) error("O APK excede o limite de segurança.")
                            digest.update(buffer, 0, read)
                            file.write(buffer, 0, read)
                            if (expected > 0) {
                                // 100% fica reservado para depois de validar
                                // hash, pacote, versão e certificado.
                                val percent = (total * 100 / expected).toInt().coerceAtMost(99)
                                if (percent != lastPercent) {
                                    lastPercent = percent
                                    onProgress(percent)
                                }
                            }
                        }
                        file.fd.sync()
                    }
                }
                if (expected >= 0 && total != expected) error("O download da atualização ficou incompleto.")
                val actual = digest.digest().joinToString("") { "%02x".format(it) }
                if (actual != manifest.sha256) error("O APK baixado não passou na verificação de segurança.")
                ApkIntegrity.verifyArchive(activity.applicationContext, partial, manifest.latestVersionCode)
                activeJob?.ensureActive()
                synchronized(FILE_LOCK) {
                    try {
                        Files.move(
                            partial.toPath(),
                            output.toPath(),
                            StandardCopyOption.ATOMIC_MOVE,
                            StandardCopyOption.REPLACE_EXISTING,
                        )
                    } catch (_: AtomicMoveNotSupportedException) {
                        Files.move(
                            partial.toPath(),
                            output.toPath(),
                            StandardCopyOption.REPLACE_EXISTING,
                        )
                    }
                }
                onProgress(100)
            }
            output
        } catch (error: Exception) {
            partial.delete()
            throw error
        } finally {
            cancellation?.dispose()
        }
    }

    fun openInstaller(apk: File) {
        val uri = FileProvider.getUriForFile(
            activity,
            "${activity.packageName}.files",
            apk,
        )
        activity.startActivity(
            Intent(Intent.ACTION_VIEW).apply {
                setDataAndType(uri, "application/vnd.android.package-archive")
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            },
        )
    }

    companion object {
        private const val MAX_APK_BYTES = 160L * 1024 * 1024
        private const val OFFICIAL_HOST = "github.com"
        private const val OFFICIAL_PATH_PREFIX =
            "/XardAly/slucssTube-releases-Tube/releases/download/"
        private val FILE_LOCK = Any()

        internal fun isOfficialDownloadUrl(value: String): Boolean {
            val url = value.toHttpUrlOrNull() ?: return false
            val api = BuildConfig.API_BASE_URL.toHttpUrlOrNull()
            return url.scheme == "https" && url.username.isEmpty() && url.password.isEmpty() &&
                ((url.host == OFFICIAL_HOST && url.port == 443 && url.encodedPath.startsWith(OFFICIAL_PATH_PREFIX)) ||
                    (url.host == api?.host && url.port == api.port && url.encodedPath.startsWith("/app/android/files/")))
        }
    }
}
