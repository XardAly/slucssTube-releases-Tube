package com.xard.ytsystem.upscale

import android.media.MediaCodecInfo
import android.media.MediaCodecList
import android.media.MediaFormat
import android.os.Build
import kotlin.math.roundToInt

data class EncoderChoice(
    val codecName: String,
    val mime: String,
    val bitrate: Int,
    val frameRate: Double,
)

object CodecSelector {
    private data class CompatibleEncoder(
        val codec: MediaCodecInfo,
        val bitrateRange: android.util.Range<Int>,
        val frameRate: Double,
    )

    fun choose(
        width: Int,
        height: Int,
        fps: Double,
        requestedFormat: String,
        sourceBitrate: Int,
        scale: Int,
    ): EncoderChoice {
        require(width > 0 && height > 0 && width % 2 == 0 && height % 2 == 0) {
            "A resolução final precisa ter dimensões pares para o encoder do Android."
        }
        val mimeCandidates = when (requestedFormat) {
            "hevc" -> listOf(MediaFormat.MIMETYPE_VIDEO_HEVC)
            "mp4", "auto" -> listOf(
                MediaFormat.MIMETYPE_VIDEO_AVC,
                MediaFormat.MIMETYPE_VIDEO_HEVC,
            )
            else -> throw IllegalArgumentException("Formato de saída inválido.")
        }
        val codecs = MediaCodecList(MediaCodecList.ALL_CODECS).codecInfos
            .filter { it.isEncoder }
        for (mime in mimeCandidates) {
            val compatible = codecs.mapNotNull { codec ->
                val caps = runCatching { codec.getCapabilitiesForType(mime) }.getOrNull()
                    ?: return@mapNotNull null
                if (!caps.colorFormats.contains(
                        MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible,
                    )
                ) return@mapNotNull null
                val video = caps.videoCapabilities ?: return@mapNotNull null
                if (!runCatching { video.isSizeSupported(width, height) }.getOrDefault(false)) {
                    return@mapNotNull null
                }
                val supportedRange = runCatching {
                    video.getSupportedFrameRatesFor(width, height)
                }.getOrNull()
                val selectedFrameRate = encoderFrameRateCandidates(
                    fps,
                    supportedRange?.upper,
                ).firstOrNull { candidate ->
                    runCatching {
                        video.areSizeAndRateSupported(width, height, candidate)
                    }.getOrDefault(false)
                } ?: return@mapNotNull null
                CompatibleEncoder(codec, video.bitrateRange, selectedFrameRate)
            }.sortedWith(
                compareBy<CompatibleEncoder> { if (isHardware(it.codec)) 0 else 1 }
                    .thenByDescending { it.frameRate },
            )
            val selected = compatible.firstOrNull() ?: continue
            val pixelRate = width.toLong() * height * selected.frameRate.coerceAtLeast(1.0)
            val qualityTarget = (pixelRate * if (mime == MediaFormat.MIMETYPE_VIDEO_HEVC) 0.07 else 0.11)
                .roundToInt()
            val scaledSource = sourceBitrate.takeIf { it > 0 }
                ?.toLong()?.times(scale.toLong() * scale)?.times(3)?.div(4)?.toInt()
                ?: 0
            val desired = maxOf(qualityTarget, scaledSource, 4_000_000)
            return EncoderChoice(
                codecName = selected.codec.name,
                mime = mime,
                bitrate = desired.coerceIn(
                    selected.bitrateRange.lower.coerceAtLeast(1_000_000),
                    selected.bitrateRange.upper,
                ),
                frameRate = selected.frameRate,
            )
        }
        throw IllegalStateException(
            "Nenhum encoder H.264/HEVC do aparelho suporta ${width}×${height} " +
                "com entrada YUV, nem reduzindo automaticamente os ${fps.roundToInt()} FPS.",
        )
    }

    private fun isHardware(codec: MediaCodecInfo): Boolean {
        if (Build.VERSION.SDK_INT >= 29) return codec.isHardwareAccelerated
        val name = codec.name.lowercase()
        return listOf("omx.google.", "c2.android.", "c2.google.").none(name::startsWith)
    }
}

internal fun encoderFrameRateCandidates(
    requestedFrameRate: Double,
    reportedMaximumFrameRate: Double? = null,
): List<Double> {
    val requested = requestedFrameRate.takeIf { it.isFinite() && it > 0.0 } ?: 30.0
    val maximum = reportedMaximumFrameRate
        ?.takeIf { it.isFinite() && it > 0.0 }
        ?.coerceAtMost(requested)
        ?: requested
    return buildList {
        add(maximum)
        for (candidate in listOf(120.0, 60.0, 59.94, 50.0, 30.0, 29.97, 25.0, 24.0, 20.0, 15.0)) {
            if (candidate <= maximum + 0.001) add(candidate)
        }
    }.distinctBy { (it * 1_000).roundToInt() }
        .sortedDescending()
}
