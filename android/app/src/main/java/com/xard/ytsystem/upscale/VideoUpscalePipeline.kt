package com.xard.ytsystem.upscale

import android.content.Context
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaExtractor
import android.media.MediaFormat
import android.media.MediaMetadataRetriever
import android.media.MediaMuxer
import android.net.Uri
import android.os.SystemClock
import java.io.File
import java.nio.ByteBuffer
import java.util.ArrayDeque
import kotlin.math.max
import kotlin.math.roundToInt

data class UpscaleProgress(
    val stage: String,
    val processedFrames: Int,
    val totalFrames: Int,
    val percent: Float,
    val etaSeconds: Long,
    val message: String,
)

data class UpscaleResult(
    val output: File,
    val usedVulkan: Boolean,
    val gpuName: String,
    val encoderName: String,
    val outputMime: String,
    val processedFrames: Int,
    val sourceFrameRate: Double,
    val outputFrameRate: Double,
    val elapsedMillis: Long,
    val maximumThermalStatus: Int,
    val batteryUsedPercent: Int?,
)

data class VideoProbe(
    val durationMillis: Long,
    val width: Int,
    val height: Int,
    val rotation: Int,
)

class VideoUpscalePipeline(
    private val context: Context,
    private val source: Uri,
    private val output: File,
    private val options: UpscaleOptions,
    private val shouldCancel: () -> Boolean,
    private val onProgress: (UpscaleProgress) -> Unit,
    private val onEngineChanged: (NativeUpscaler?) -> Unit,
) {
    private val model = UpscaleModel.fromApi(options.model)

    fun run(): UpscaleResult {
        val started = SystemClock.elapsedRealtime()
        val capabilities = DeviceCapabilities.inspect(context)
        val files = runBlockingModelPreparation()
        val extractor = MediaExtractor()
        var decoder: MediaCodec? = null
        var encoder: MediaCodec? = null
        var muxer: MediaMuxer? = null
        var muxerStarted = false
        var engine: NativeUpscaler? = null
        val thermal = ThermalGuard(context)
        val batteryStart = thermal.batteryPercent.takeIf { it in 0..100 }
        try {
            extractor.setDataSource(context, source, null)
            val videoTrack = findTrack(extractor, "video/")
            if (videoTrack < 0) error("O arquivo selecionado não possui uma faixa de vídeo.")
            val sourceFormat = extractor.getTrackFormat(videoTrack)
            val sourceMime = sourceFormat.string(MediaFormat.KEY_MIME)
            val sourceWidth = displayDimension(sourceFormat, true)
            val sourceHeight = displayDimension(sourceFormat, false)
            val outputWidth = Math.multiplyExact(sourceWidth, options.scale)
            val outputHeight = Math.multiplyExact(sourceHeight, options.scale)
            val encoderFrameSize = Math.toIntExact(
                Math.multiplyExact(outputWidth.toLong(), outputHeight.toLong()) * 3L / 2L,
            )
            val durationUs = sourceFormat.longOr(MediaFormat.KEY_DURATION, 0L)
            val totalFrames = countFrames(videoTrack)
            val fps = sourceFormat.numberOr(MediaFormat.KEY_FRAME_RATE)?.toDouble()
                ?.takeIf { it > 0.0 }
                ?: if (durationUs > 0 && totalFrames > 1) totalFrames * 1_000_000.0 / durationUs else 30.0
            ensureMemory(capabilities, sourceWidth, sourceHeight, outputWidth, outputHeight)
            val encoderChoice = CodecSelector.choose(
                outputWidth,
                outputHeight,
                fps,
                options.outputFormat,
                sourceFormat.intOr(MediaFormat.KEY_BIT_RATE, 0),
                options.scale,
            )
            val colorStandard = sourceFormat.intOr(
                MediaFormat.KEY_COLOR_STANDARD,
                if (sourceWidth >= 1280 || sourceHeight >= 720) {
                    MediaFormat.COLOR_STANDARD_BT709
                } else {
                    MediaFormat.COLOR_STANDARD_BT601_NTSC
                },
            )
            val colorRange = sourceFormat.intOr(
                MediaFormat.KEY_COLOR_RANGE,
                MediaFormat.COLOR_RANGE_LIMITED,
            )
            val colorTransfer = sourceFormat.intOr(
                MediaFormat.KEY_COLOR_TRANSFER,
                MediaFormat.COLOR_TRANSFER_SDR_VIDEO,
            )

            val encoderFormat = MediaFormat.createVideoFormat(
                encoderChoice.mime,
                outputWidth,
                outputHeight,
            ).apply {
                setInteger(
                    MediaFormat.KEY_COLOR_FORMAT,
                    MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible,
                )
                setInteger(MediaFormat.KEY_BIT_RATE, encoderChoice.bitrate)
                setFloat(MediaFormat.KEY_FRAME_RATE, encoderChoice.frameRate.toFloat())
                setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 2)
                setInteger(MediaFormat.KEY_COLOR_STANDARD, colorStandard)
                setInteger(MediaFormat.KEY_COLOR_RANGE, colorRange)
                setInteger(MediaFormat.KEY_COLOR_TRANSFER, colorTransfer)
            }
            encoder = MediaCodec.createByCodecName(encoderChoice.codecName).apply {
                configure(encoderFormat, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
                start()
            }

            val decoderFormat = sourceFormat.apply {
                setInteger(
                    MediaFormat.KEY_COLOR_FORMAT,
                    MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible,
                )
            }
            decoder = MediaCodec.createDecoderByType(sourceMime).apply {
                configure(decoderFormat, null, null, 0)
                start()
            }
            extractor.selectTrack(videoTrack)

            muxer = MediaMuxer(output.absolutePath, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
            val rotation = sourceFormat.intOr(MediaFormat.KEY_ROTATION, probe(context, source).rotation)
            if (rotation != 0) muxer.setOrientationHint(rotation)
            val audioTrack = findTrack(extractor, "audio/")
            val muxerAudioTrack = if (audioTrack >= 0) {
                runCatching { muxer.addTrack(extractor.getTrackFormat(audioTrack)) }.getOrElse {
                    throw IllegalStateException(
                        "A faixa de áudio original não é compatível com MP4 neste Android.",
                        it,
                    )
                }
            } else {
                -1
            }

            fun createEngine(preferVulkan: Boolean): NativeUpscaler = NativeUpscaler(
                model = model,
                scale = options.scale,
                files = files,
                preferVulkan = preferVulkan,
                threads = capabilities.inferenceThreads,
                tileSize = capabilities.tileSize(model, options.scale),
            ).also(onEngineChanged)

            engine = runCatching { createEngine(capabilities.canUseVulkan) }
                .getOrElse { firstError ->
                    if (!capabilities.canUseVulkan) throw firstError
                    createEngine(false)
                }
            var usedVulkan = engine.usesVulkan
            val frameRateNotice = if (encoderChoice.frameRate + 0.001 < fps) {
                val outputFps = (encoderChoice.frameRate * 100).roundToInt() / 100.0
                " · saída limitada a $outputFps FPS pelo encoder"
            } else {
                ""
            }
            onProgress(
                UpscaleProgress(
                    "processing", 0, totalFrames, 0f, -1,
                    "Iniciando ${model.displayName} com " +
                        "${if (usedVulkan) "Vulkan" else "CPU"}$frameRateNotice...",
                ),
            )

            val decoderInfo = MediaCodec.BufferInfo()
            val encoderInfo = MediaCodec.BufferInfo()
            var decoderInputEnded = false
            var decoderOutputEnded = false
            var encoderInputEnded = false
            var encoderOutputEnded = false
            var muxerVideoTrack = -1
            var processed = 0
            var encodedFrames = 0
            var lastPts = 0L
            val samples = ArrayDeque<Pair<Int, Long>>()
            val frameRateLimiter = FrameRateLimiter(fps, encoderChoice.frameRate)

            fun drainEncoder(wait: Boolean) {
                while (!encoderOutputEnded) {
                    val outputIndex = encoder.dequeueOutputBuffer(
                        encoderInfo,
                        if (wait) CODEC_TIMEOUT_US else 0L,
                    )
                    when {
                        outputIndex == MediaCodec.INFO_TRY_AGAIN_LATER -> return
                        outputIndex == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> {
                            check(muxerVideoTrack < 0) { "O encoder alterou o formato duas vezes." }
                            muxerVideoTrack = muxer.addTrack(encoder.outputFormat)
                            muxer.start()
                            muxerStarted = true
                        }
                        outputIndex >= 0 -> {
                            val encoded = encoder.getOutputBuffer(outputIndex)
                            if (encoded != null && encoderInfo.size > 0 &&
                                encoderInfo.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG == 0
                            ) {
                                check(muxerStarted && muxerVideoTrack >= 0) {
                                    "O encoder não informou o formato de saída."
                                }
                                encoded.position(encoderInfo.offset)
                                encoded.limit(encoderInfo.offset + encoderInfo.size)
                                muxer.writeSampleData(muxerVideoTrack, encoded, encoderInfo)
                            }
                            encoderOutputEnded = encoderInfo.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0
                            encoder.releaseOutputBuffer(outputIndex, false)
                        }
                    }
                }
            }

            fun encoderInputIndex(): Int {
                while (true) {
                    checkCancellation()
                    drainEncoder(false)
                    val index = encoder.dequeueInputBuffer(CODEC_TIMEOUT_US)
                    if (index >= 0) return index
                }
            }

            while (!decoderOutputEnded) {
                checkCancellation()
                if (!decoderInputEnded) {
                    val inputIndex = decoder.dequeueInputBuffer(CODEC_TIMEOUT_US)
                    if (inputIndex >= 0) {
                        val input = decoder.getInputBuffer(inputIndex)
                            ?: error("O decoder não forneceu buffer de entrada.")
                        val size = extractor.readSampleData(input, 0)
                        if (size < 0) {
                            decoder.queueInputBuffer(
                                inputIndex, 0, 0, 0L, MediaCodec.BUFFER_FLAG_END_OF_STREAM,
                            )
                            decoderInputEnded = true
                        } else {
                            val sampleFlags = extractor.sampleFlags
                            if (sampleFlags and MediaExtractor.SAMPLE_FLAG_ENCRYPTED != 0) {
                                error("Vídeos protegidos por DRM não podem ser processados localmente.")
                            }
                            decoder.queueInputBuffer(
                                inputIndex,
                                0,
                                size,
                                extractor.sampleTime,
                                mediaCodecInputFlags(sampleFlags),
                            )
                            extractor.advance()
                        }
                    }
                }

                val outputIndex = decoder.dequeueOutputBuffer(decoderInfo, CODEC_TIMEOUT_US)
                if (outputIndex >= 0) {
                    val end = decoderInfo.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0
                    if (decoderInfo.size > 0) {
                        if (frameRateLimiter.shouldEncode(decoderInfo.presentationTimeUs)) {
                            thermal.beforeFrame()
                            val sourceImage = decoder.getOutputImage(outputIndex)
                                ?: error("O decoder não disponibilizou o frame em YUV.")
                            val encoderInput = encoderInputIndex()
                            val targetImage = encoder.getInputImage(encoderInput)
                                ?: error("O encoder não aceitou entrada YUV flexível.")
                            var encoderInputQueued = false
                            try {
                                val currentEngine = checkNotNull(engine)
                                try {
                                    currentEngine.process(
                                        sourceImage, targetImage, colorStandard, colorRange,
                                    )
                                } catch (error: Exception) {
                                    if (!currentEngine.usesVulkan || error is UpscaleCancelled) throw error
                                    currentEngine.close()
                                    val cpuEngine = createEngine(false)
                                    engine = cpuEngine
                                    usedVulkan = false
                                    checkCancellation()
                                    cpuEngine.process(sourceImage, targetImage, colorStandard, colorRange)
                                }
                                encoder.queueInputBuffer(
                                    encoderInput,
                                    0,
                                    encoderFrameSize,
                                    decoderInfo.presentationTimeUs,
                                    0,
                                )
                                encoderInputQueued = true
                            } finally {
                                if (!encoderInputQueued) targetImage.close()
                                sourceImage.close()
                            }
                            lastPts = max(lastPts, decoderInfo.presentationTimeUs)
                            encodedFrames++
                            drainEncoder(false)
                        }
                        processed++
                        val now = SystemClock.elapsedRealtime()
                        samples.addLast(processed to now)
                        while (samples.size > ETA_WINDOW) samples.removeFirst()
                        val eta = estimateEta(samples, processed, totalFrames)
                        val percent = if (totalFrames > 0) {
                            processed * 98f / totalFrames
                        } else {
                            0f
                        }.coerceIn(0f, 98f)
                        onProgress(
                            UpscaleProgress(
                                "processing",
                                processed,
                                totalFrames,
                                percent,
                                eta,
                                buildString {
                                    append("Processando $processed")
                                    if (totalFrames > 0) append(" / $totalFrames frames")
                                    if (eta >= 0) append(" · restam ${formatEta(eta)}")
                                },
                            ),
                        )
                    }
                    decoder.releaseOutputBuffer(outputIndex, false)
                    decoderOutputEnded = end
                }
            }

            if (!encoderInputEnded) {
                val inputIndex = encoderInputIndex()
                encoder.queueInputBuffer(
                    inputIndex,
                    0,
                    0,
                    lastPts,
                    MediaCodec.BUFFER_FLAG_END_OF_STREAM,
                )
                encoderInputEnded = true
            }
            while (!encoderOutputEnded) {
                checkCancellation()
                drainEncoder(true)
            }

            if (audioTrack >= 0 && muxerAudioTrack >= 0) {
                onProgress(UpscaleProgress("muxing", processed, totalFrames, 99f, -1, "Preservando áudio..."))
                copyTrack(audioTrack, muxer, muxerAudioTrack)
            }
            val batteryEnd = thermal.batteryPercent.takeIf { it in 0..100 }
            return UpscaleResult(
                output = output,
                usedVulkan = usedVulkan,
                gpuName = capabilities.gpu.deviceName,
                encoderName = encoderChoice.codecName,
                outputMime = encoderChoice.mime,
                processedFrames = encodedFrames,
                sourceFrameRate = fps,
                outputFrameRate = encoderChoice.frameRate,
                elapsedMillis = SystemClock.elapsedRealtime() - started,
                maximumThermalStatus = thermal.maximumStatus,
                batteryUsedPercent = if (batteryStart != null && batteryEnd != null) {
                    (batteryStart - batteryEnd).coerceAtLeast(0)
                } else {
                    null
                },
            )
        } finally {
            onEngineChanged(null)
            engine?.close()
            extractor.release()
            stopCodec(decoder)
            stopCodec(encoder)
            if (muxerStarted) runCatching { muxer?.stop() }
            runCatching { muxer?.release() }
            thermal.close()
        }
    }

    private fun runBlockingModelPreparation(): ModelFiles = kotlinx.coroutines.runBlocking {
        UpscaleModelStore.prepare(context, model, options.scale)
    }

    private fun checkCancellation() {
        if (shouldCancel()) throw UpscaleCancelled()
    }

    private fun countFrames(track: Int): Int {
        val counter = MediaExtractor()
        return try {
            counter.setDataSource(context, source, null)
            counter.selectTrack(track)
            var frames = 0
            while (counter.sampleTime >= 0) {
                checkCancellation()
                frames++
                if (!counter.advance()) break
            }
            frames
        } finally {
            counter.release()
        }
    }

    private fun copyTrack(sourceTrack: Int, muxer: MediaMuxer, targetTrack: Int) {
        val audio = MediaExtractor()
        try {
            audio.setDataSource(context, source, null)
            audio.selectTrack(sourceTrack)
            val format = audio.getTrackFormat(sourceTrack)
            val capacity = format.intOr(MediaFormat.KEY_MAX_INPUT_SIZE, 1024 * 1024)
                .coerceIn(64 * 1024, 8 * 1024 * 1024)
            val buffer = ByteBuffer.allocateDirect(capacity)
            val info = MediaCodec.BufferInfo()
            while (audio.sampleTime >= 0) {
                checkCancellation()
                buffer.clear()
                val size = audio.readSampleData(buffer, 0)
                if (size < 0) break
                var muxerFlags = 0
                if (audio.sampleFlags and MediaExtractor.SAMPLE_FLAG_SYNC != 0) {
                    muxerFlags = muxerFlags or MediaCodec.BUFFER_FLAG_KEY_FRAME
                }
                if (audio.sampleFlags and MediaExtractor.SAMPLE_FLAG_PARTIAL_FRAME != 0) {
                    muxerFlags = muxerFlags or MediaCodec.BUFFER_FLAG_PARTIAL_FRAME
                }
                info.set(0, size, audio.sampleTime, muxerFlags)
                muxer.writeSampleData(targetTrack, buffer, info)
                if (!audio.advance()) break
            }
        } finally {
            audio.release()
        }
    }

    private fun findTrack(extractor: MediaExtractor, prefix: String): Int =
        (0 until extractor.trackCount).firstOrNull {
            extractor.getTrackFormat(it).string(MediaFormat.KEY_MIME).startsWith(prefix)
        } ?: -1

    private fun displayDimension(format: MediaFormat, horizontal: Boolean): Int {
        val sizeKey = if (horizontal) MediaFormat.KEY_WIDTH else MediaFormat.KEY_HEIGHT
        val startKey = if (horizontal) "crop-left" else "crop-top"
        val endKey = if (horizontal) "crop-right" else "crop-bottom"
        val coded = format.getInteger(sizeKey)
        val start = format.intOr(startKey, 0)
        val end = format.intOr(endKey, coded - 1)
        return (end - start + 1).takeIf { it in 1..coded } ?: coded
    }

    private fun ensureMemory(
        capabilities: DeviceCapabilities,
        sourceWidth: Int,
        sourceHeight: Int,
        outputWidth: Int,
        outputHeight: Int,
    ) {
        val sourceBytes = sourceWidth.toLong() * sourceHeight * 6
        val outputBytes = outputWidth.toLong() * outputHeight * 6
        val estimatedMb = (sourceBytes + outputBytes) / 1024 / 1024 + 256
        if (capabilities.lowMemory || capabilities.availableRamMb < estimatedMb.coerceAtLeast(512)) {
            throw IllegalStateException(
                "Memória disponível insuficiente para ${outputWidth}×${outputHeight}. " +
                    "Feche outros aplicativos ou use uma escala menor.",
            )
        }
    }

    private fun stopCodec(codec: MediaCodec?) {
        if (codec == null) return
        runCatching { codec.stop() }
        runCatching { codec.release() }
    }

    private fun estimateEta(
        samples: ArrayDeque<Pair<Int, Long>>,
        processed: Int,
        total: Int,
    ): Long {
        if (samples.size < ETA_MIN_SAMPLES || total <= processed) return -1
        val first = samples.first()
        val last = samples.last()
        val elapsed = (last.second - first.second) / 1000.0
        val rate = (last.first - first.first) / elapsed.coerceAtLeast(0.001)
        return ((total - processed) / rate).toLong().coerceAtLeast(0)
    }

    companion object {
        private const val CODEC_TIMEOUT_US = 10_000L
        private const val ETA_WINDOW = 30
        private const val ETA_MIN_SAMPLES = 12

        fun probe(context: Context, source: Uri): VideoProbe {
            val retriever = MediaMetadataRetriever()
            return try {
                retriever.setDataSource(context, source)
                VideoProbe(
                    durationMillis = retriever.extractMetadata(
                        MediaMetadataRetriever.METADATA_KEY_DURATION,
                    )?.toLongOrNull() ?: 0L,
                    width = retriever.extractMetadata(
                        MediaMetadataRetriever.METADATA_KEY_VIDEO_WIDTH,
                    )?.toIntOrNull() ?: 0,
                    height = retriever.extractMetadata(
                        MediaMetadataRetriever.METADATA_KEY_VIDEO_HEIGHT,
                    )?.toIntOrNull() ?: 0,
                    rotation = retriever.extractMetadata(
                        MediaMetadataRetriever.METADATA_KEY_VIDEO_ROTATION,
                    )?.toIntOrNull() ?: 0,
                )
            } finally {
                retriever.release()
            }
        }

        private fun formatEta(seconds: Long): String = when {
            seconds >= 3600 -> "%dh %02d min".format(seconds / 3600, seconds % 3600 / 60)
            seconds >= 60 -> "%d min %02d s".format(seconds / 60, seconds % 60)
            else -> "$seconds s"
        }
    }
}

internal class FrameRateLimiter(sourceFrameRate: Double, outputFrameRate: Double) {
    private val intervalUs = if (
        sourceFrameRate.isFinite() && outputFrameRate.isFinite() &&
        outputFrameRate > 0.0 && outputFrameRate + 0.001 < sourceFrameRate
    ) {
        1_000_000.0 / outputFrameRate
    } else {
        0.0
    }
    private var nextPresentationTimeUs = Double.NaN

    fun shouldEncode(presentationTimeUs: Long): Boolean {
        if (intervalUs == 0.0) return true
        if (nextPresentationTimeUs.isNaN()) {
            nextPresentationTimeUs = presentationTimeUs + intervalUs
            return true
        }
        if (presentationTimeUs + TIMESTAMP_TOLERANCE_US < nextPresentationTimeUs) return false
        do {
            nextPresentationTimeUs += intervalUs
        } while (nextPresentationTimeUs <= presentationTimeUs + TIMESTAMP_TOLERANCE_US)
        return true
    }

    private companion object {
        const val TIMESTAMP_TOLERANCE_US = 500.0
    }
}

private fun MediaFormat.string(key: String): String = getString(key).orEmpty()

private fun MediaFormat.intOr(key: String, fallback: Int): Int =
    if (containsKey(key)) runCatching { getInteger(key) }.getOrDefault(fallback) else fallback

private fun MediaFormat.longOr(key: String, fallback: Long): Long =
    if (containsKey(key)) runCatching { getLong(key) }.getOrDefault(fallback) else fallback

private fun MediaFormat.numberOr(key: String): Number? = if (containsKey(key)) {
    runCatching { getFloat(key) }.getOrElse {
        runCatching { getInteger(key) }.getOrNull()
    }
} else {
    null
}

internal fun mediaCodecInputFlags(sampleFlags: Int): Int {
    var result = 0
    if (sampleFlags and MediaExtractor.SAMPLE_FLAG_SYNC != 0) {
        result = result or MediaCodec.BUFFER_FLAG_KEY_FRAME
    }
    if (sampleFlags and MediaExtractor.SAMPLE_FLAG_PARTIAL_FRAME != 0) {
        result = result or MediaCodec.BUFFER_FLAG_PARTIAL_FRAME
    }
    return result
}
