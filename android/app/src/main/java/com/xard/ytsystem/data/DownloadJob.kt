package com.xard.ytsystem.data

import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "download_jobs")
data class DownloadJob(
    @PrimaryKey val id: String,
    val type: String,
    val url: String? = null,
    val sourceUri: String? = null,
    val title: String,
    val mode: String = "va",
    val height: Int? = null,
    val videoFormat: String = "mp4",
    val audioFormat: String = "mp3",
    val optionsJson: String? = null,
    val operationId: String = "",
    val status: String = STATUS_QUEUED,
    val stage: String = "queued",
    val progress: Float = 0f,
    val downloadedBytes: Long = 0,
    val totalBytes: Long = 0,
    val speedBytes: Long = 0,
    val etaSeconds: Long = -1,
    val message: String = "Aguardando na fila.",
    val outputUri: String? = null,
    val outputMime: String? = null,
    val outputName: String? = null,
    val error: String? = null,
    // Texto técnico da falha (etapa, exceção e últimas linhas da ferramenta).
    // Fica separado de `error`, que é a frase mostrada na lista: este aqui é o
    // que a pessoa copia e envia no suporte.
    val errorDetail: String? = null,
    val fallbackEligible: Boolean = false,
    val createdAt: Long = System.currentTimeMillis(),
    val updatedAt: Long = System.currentTimeMillis(),
) {
    companion object {
        const val TYPE_DOWNLOAD = "download"
        const val TYPE_GIF = "gif"
        const val TYPE_COMPATIBILITY = "compatibility"
        const val TYPE_UPSCALE = "upscale"
        const val TYPE_SERVER_DOWNLOAD = "server_download"
        const val STATUS_QUEUED = "queued"
        const val STATUS_RUNNING = "running"
        const val STATUS_COMPLETED = "completed"
        const val STATUS_FAILED = "failed"
        const val STATUS_CANCELLED = "cancelled"
    }
}
