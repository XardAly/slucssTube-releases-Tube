package com.xard.ytsystem.identity

import android.content.Context
import android.os.Build
import com.google.gson.JsonObject
import com.xard.ytsystem.BuildConfig
import com.xard.ytsystem.data.DownloadJob
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.launch
import java.time.OffsetDateTime

/** Bounded, memory-only telemetry. Its lifetime is independent from the foreground service. */
class DownloadEvents(context: Context, private val send: suspend (JsonObject) -> Unit) {
    private val names = LocalNameStore(context)
    private val events = Channel<JsonObject>(32)
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    init {
        scope.launch {
            for (event in events) {
                try { send(event) }
                catch (cancelled: CancellationException) { throw cancelled }
                catch (_: Exception) { /* No disk queue, name logs or impact on the download. */ }
            }
        }
    }

    fun currentName(): String = names.name

    fun emit(job: DownloadJob, status: String, name: String, error: String? = null, fileName: String? = null) {
        if (job.type !in setOf(DownloadJob.TYPE_DOWNLOAD, DownloadJob.TYPE_SERVER_DOWNLOAD) || name.isBlank()) return
        events.trySend(payload(job, status, name, error, fileName))
    }

    companion object {
        internal fun payload(job: DownloadJob, status: String, name: String, error: String?, fileName: String?): JsonObject =
            JsonObject().apply {
                addProperty("event", "download_$status")
                add("user", JsonObject().apply { addProperty("name", name.take(60)) })
                add("device", JsonObject().apply {
                    addProperty("manufacturer", Build.MANUFACTURER?.trim().orEmpty().ifBlank { "Unknown Device" }.take(100))
                    addProperty("model", Build.MODEL?.trim().orEmpty().ifBlank { "Unknown Device" }.take(100))
                    addProperty("android_version", Build.VERSION.RELEASE?.take(40).orEmpty().ifBlank { "Unknown" })
                })
                add("app", JsonObject().apply { addProperty("version", BuildConfig.VERSION_NAME) })
                add("download", JsonObject().apply {
                    addProperty("title", job.title.take(200))
                    addProperty("format", (if (job.mode == "a") job.audioFormat else job.videoFormat).uppercase())
                    addProperty("quality", if (job.mode == "a") "Apenas áudio" else job.height?.let { "${it}p" } ?: "Melhor disponível")
                    addProperty("status", status)
                    fileName?.let { addProperty("file_name", it.take(240)) }
                    error?.let { addProperty("error", it.take(300)) }
                })
                addProperty("timestamp", OffsetDateTime.now().toString())
            }
    }
}
