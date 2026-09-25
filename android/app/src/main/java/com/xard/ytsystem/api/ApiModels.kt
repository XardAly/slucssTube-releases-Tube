package com.xard.ytsystem.api

data class LocalPermit(
    val operationId: String,
    val expiresAt: Long,
)

data class AndroidManifest(
    val latestVersion: String,
    val latestVersionCode: Int,
    val minimumVersionCode: Int,
    val downloadUrl: String,
    val sha256: String,
    val changelog: List<String>,
    val processingMode: String,
    val serverFallbackEnabled: Boolean,
    val upscaleEnabled: Boolean,
    val upscaleMinimumVersionCode: Int,
    val upscaleEngineVersion: String,
    val animeVideoV3ModelVersion: String,
    val realCuganModelVersion: String,
    val releaseId: String = "",
    val publishedAt: String = "",
    val updatePolicy: String = "recommended",
)

/**
 * Metadados vindos da API, usados quando a extração local é recusada.
 *
 * A API entrega as qualidades já agrupadas por altura; o aparelho nunca recebe a
 * lista bruta de formatos do yt-dlp.
 */
data class RemoteVideoInfo(
    val title: String,
    val uploader: String?,
    val duration: Int,
    val heights: List<Int>,
    val thumbnail: String?,
)

data class ServerDownloadStatus(
    val id: String,
    val status: String,
    val progress: Float,
    val stage: String,
    val message: String,
    val fileUrl: String?,
    val fileName: String?,
    val error: String?,
)

data class GifOptions(
    val start: Double,
    val end: Double,
    val resolution: String = "480p",
    val customWidth: Int = 640,
    val customHeight: Int = 480,
    val fps: Int = 15,
    val colors: Int = 128,
    val loop: String = "infinite",
    val loopCount: Int = 1,
    val speed: Double = 1.0,
    val enhancement: String = "off",
    val antialias: Boolean = true,
    val brightness: Int = 0,
    val contrast: Int = 0,
    val saturation: Int = 0,
    val gamma: Double = 1.0,
    val sharpen: Int = 0,
    val denoise: String = "off",
    val dither: String = "auto",
    val maxSizeMb: Int? = null,
)

class ApiException(
    val statusCode: Int,
    override val message: String,
) : Exception(message)
