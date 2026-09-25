package com.xard.ytsystem.upscale

import android.media.Image
import java.io.Closeable
import java.nio.ByteBuffer

data class NativeGpuInfo(
    val gpuCount: Int,
    val deviceName: String,
    val heapBudgetMb: Int,
)

class NativeUpscaler(
    model: UpscaleModel,
    scale: Int,
    files: ModelFiles,
    preferVulkan: Boolean,
    threads: Int,
    tileSize: Int,
) : Closeable {
    @Volatile private var handle = nativeCreate(
        model.nativeValue,
        scale,
        files.param.absolutePath,
        files.weights.absolutePath,
        preferVulkan,
        threads,
        tileSize,
    )

    val usesVulkan: Boolean
        get() = handle.takeIf { it != 0L }?.let(::nativeUsesVulkan) == true

    fun process(source: Image, target: Image, colorStandard: Int, colorRange: Int) {
        val active = handle
        check(active != 0L) { "O engine de Upscale já foi encerrado." }
        require(source.planes.size == 3 && target.planes.size == 3) {
            "O codec não forneceu frames YUV compatíveis."
        }
        val sourcePlanes = source.planes
        val targetPlanes = target.planes
        val crop = source.cropRect
        val result = nativeProcess(
            active,
            directSlice(sourcePlanes[0].buffer),
            directSlice(sourcePlanes[1].buffer),
            directSlice(sourcePlanes[2].buffer),
            sourcePlanes[0].rowStride,
            sourcePlanes[1].rowStride,
            sourcePlanes[2].rowStride,
            sourcePlanes[0].pixelStride,
            sourcePlanes[1].pixelStride,
            sourcePlanes[2].pixelStride,
            crop.left,
            crop.top,
            crop.width(),
            crop.height(),
            directSlice(targetPlanes[0].buffer),
            directSlice(targetPlanes[1].buffer),
            directSlice(targetPlanes[2].buffer),
            targetPlanes[0].rowStride,
            targetPlanes[1].rowStride,
            targetPlanes[2].rowStride,
            targetPlanes[0].pixelStride,
            targetPlanes[1].pixelStride,
            targetPlanes[2].pixelStride,
            colorStandard,
            colorRange,
        )
        when (result) {
            0 -> Unit
            -20 -> throw UpscaleCancelled()
            -2, -3 -> error("O layout YUV deste codec não é compatível com o Upscale.")
            -12 -> error("Memória insuficiente durante o Upscale.")
            else -> error("O engine nativo falhou ao processar o frame (código $result).")
        }
    }

    @Synchronized
    fun cancel() {
        handle.takeIf { it != 0L }?.let(::nativeCancel)
    }

    @Synchronized
    override fun close() {
        val active = handle
        if (active == 0L) return
        handle = 0L
        nativeDestroy(active)
    }

    private fun directSlice(buffer: ByteBuffer): ByteBuffer = buffer.duplicate().slice().also {
        check(it.isDirect) { "O codec retornou um buffer que não pode ser compartilhado com JNI." }
    }

    private external fun nativeCreate(
        model: Int,
        scale: Int,
        paramPath: String,
        binPath: String,
        preferVulkan: Boolean,
        threads: Int,
        tileSize: Int,
    ): Long

    private external fun nativeUsesVulkan(handle: Long): Boolean
    private external fun nativeCancel(handle: Long)
    private external fun nativeDestroy(handle: Long)

    @Suppress("LongParameterList")
    private external fun nativeProcess(
        handle: Long,
        sourceY: ByteBuffer,
        sourceU: ByteBuffer,
        sourceV: ByteBuffer,
        sourceYRow: Int,
        sourceURow: Int,
        sourceVRow: Int,
        sourceYPixel: Int,
        sourceUPixel: Int,
        sourceVPixel: Int,
        cropLeft: Int,
        cropTop: Int,
        width: Int,
        height: Int,
        targetY: ByteBuffer,
        targetU: ByteBuffer,
        targetV: ByteBuffer,
        targetYRow: Int,
        targetURow: Int,
        targetVRow: Int,
        targetYPixel: Int,
        targetUPixel: Int,
        targetVPixel: Int,
        colorStandard: Int,
        colorRange: Int,
    ): Int

    companion object {
        init {
            System.loadLibrary("slucss_upscale")
        }

        fun gpuInfo(): NativeGpuInfo = runCatching {
            val parts = nativeGpuInfo().split('|', limit = 3)
            NativeGpuInfo(
                gpuCount = parts.getOrNull(0)?.toIntOrNull() ?: 0,
                deviceName = parts.getOrNull(1).orEmpty(),
                heapBudgetMb = parts.getOrNull(2)?.toIntOrNull() ?: 0,
            )
        }.getOrElse { NativeGpuInfo(0, "", 0) }

        @JvmStatic private external fun nativeGpuInfo(): String
    }
}

class UpscaleCancelled : Exception()
