package com.xard.ytsystem.upscale

import android.content.Context
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import java.io.FileOutputStream
import java.security.MessageDigest

enum class UpscaleModel(
    val apiValue: String,
    val displayName: String,
    val description: String,
    internal val nativeValue: Int,
    val modelVersion: String,
) {
    ANIME_VIDEO_V3(
        apiValue = "realesr_animevideov3",
        displayName = "AnimeVideoV3",
        description = "Alta qualidade com ótimo desempenho. Recomendado para a maioria dos edits e vídeos.",
        nativeValue = 0,
        modelVersion = "animevideov3-ncnn-v1",
    ),
    REAL_CUGAN(
        apiValue = "realcugan",
        displayName = "Real-CUGAN",
        description = "Máxima preservação de detalhes, linhas e texturas. Mais pesado e pode demorar significativamente mais.",
        nativeValue = 1,
        modelVersion = "realcugan-se-conservative-v1",
    );

    companion object {
        fun fromApi(value: String): UpscaleModel = entries.firstOrNull { it.apiValue == value }
            ?: throw IllegalArgumentException("Modelo de Upscale inválido.")
    }
}

data class UpscaleOptions(
    val model: String = UpscaleModel.ANIME_VIDEO_V3.apiValue,
    val scale: Int = 2,
    val outputFormat: String = "auto",
    val modelVersion: String = UpscaleModel.ANIME_VIDEO_V3.modelVersion,
    val engineVersion: String = UpscaleVersions.ENGINE_VERSION,
)

object UpscaleVersions {
    const val ENGINE_VERSION = "ncnn-20260526-v1"
    const val ASSET_VERSION = "upscale-models-v1"
}

data class ModelFiles(val param: File, val weights: File)

object UpscaleModelStore {
    private data class Asset(
        val path: String,
        val fileName: String,
        val sha256: String,
    )

    private val assets = mapOf(
        "${UpscaleModel.ANIME_VIDEO_V3.apiValue}:2:param" to Asset(
            "upscale/animevideov3/realesr-animevideov3-x2.param",
            "realesr-animevideov3-x2.param",
            "b88ff4f00ebf019a7fdac17fdd45a7fd3665d37509efc5baf2e4da2e24420a04",
        ),
        "${UpscaleModel.ANIME_VIDEO_V3.apiValue}:2:bin" to Asset(
            "upscale/animevideov3/realesr-animevideov3-x2.bin",
            "realesr-animevideov3.bin",
            "548a36f9c3f4ab8da56cd3b13badf23968bee207b396dad14d04b830e5f2ab2d",
        ),
        "${UpscaleModel.ANIME_VIDEO_V3.apiValue}:3:param" to Asset(
            "upscale/animevideov3/realesr-animevideov3-x3.param",
            "realesr-animevideov3-x3.param",
            "d1a5755008791d09b57e3425fc9dd0bd26b00fdf79c606210bc0e693f8230881",
        ),
        "${UpscaleModel.ANIME_VIDEO_V3.apiValue}:3:bin" to Asset(
            "upscale/animevideov3/realesr-animevideov3-x2.bin",
            "realesr-animevideov3.bin",
            "548a36f9c3f4ab8da56cd3b13badf23968bee207b396dad14d04b830e5f2ab2d",
        ),
        "${UpscaleModel.ANIME_VIDEO_V3.apiValue}:4:param" to Asset(
            "upscale/animevideov3/realesr-animevideov3-x4.param",
            "realesr-animevideov3-x4.param",
            "850a248e7c14c27e5bd8cf7265113a9441036a7db63963bb8aa5169d788a435e",
        ),
        "${UpscaleModel.ANIME_VIDEO_V3.apiValue}:4:bin" to Asset(
            "upscale/animevideov3/realesr-animevideov3-x2.bin",
            "realesr-animevideov3.bin",
            "548a36f9c3f4ab8da56cd3b13badf23968bee207b396dad14d04b830e5f2ab2d",
        ),
        "${UpscaleModel.REAL_CUGAN.apiValue}:2:param" to Asset(
            "upscale/realcugan-se/up2x-conservative.param",
            "up2x-conservative.param",
            "91efac7489bf249f092faa3764e3a3d1d31ef290051e39f2afd2138c98ccce30",
        ),
        "${UpscaleModel.REAL_CUGAN.apiValue}:2:bin" to Asset(
            "upscale/realcugan-se/up2x-conservative.bin",
            "up2x-conservative.bin",
            "91c72c136e7ff8556323d4449c5ceeff22d9829bf8463a01137cadb8d59b84a0",
        ),
        "${UpscaleModel.REAL_CUGAN.apiValue}:3:param" to Asset(
            "upscale/realcugan-se/up3x-conservative.param",
            "up3x-conservative.param",
            "9e71ba0bce194f6a95422140c8caa25accbbfd9ec84a0cc08f7f4ec1e615a5a8",
        ),
        "${UpscaleModel.REAL_CUGAN.apiValue}:3:bin" to Asset(
            "upscale/realcugan-se/up3x-conservative.bin",
            "up3x-conservative.bin",
            "8321c450efedcd94192196b03ddec19c2a023a5637df7208d49e9e0461560dff",
        ),
        "${UpscaleModel.REAL_CUGAN.apiValue}:4:param" to Asset(
            "upscale/realcugan-se/up4x-conservative.param",
            "up4x-conservative.param",
            "63e8f6a5a57acb4b7678fd928612538ae7208c836223becdb66ffb82bd2aab9b",
        ),
        "${UpscaleModel.REAL_CUGAN.apiValue}:4:bin" to Asset(
            "upscale/realcugan-se/up4x-conservative.bin",
            "up4x-conservative.bin",
            "1ffde324bf3444adade3e70ff4ff4eac7a4bfbf4c01b5aab4563a89e5cec16da",
        ),
    )

    suspend fun prepare(context: Context, model: UpscaleModel, scale: Int): ModelFiles =
        withContext(Dispatchers.IO) {
            require(scale in 2..4) { "Escala de Upscale inválida." }
            val param = requireNotNull(assets["${model.apiValue}:$scale:param"])
            val weights = requireNotNull(assets["${model.apiValue}:$scale:bin"])
            val directory = File(context.noBackupFilesDir, "models/${UpscaleVersions.ASSET_VERSION}")
                .apply { if (!isDirectory && !mkdirs()) error("Não foi possível preparar os modelos.") }
            ModelFiles(
                copyVerified(context, param, directory),
                copyVerified(context, weights, directory),
            )
        }

    private fun copyVerified(context: Context, asset: Asset, directory: File): File {
        val destination = File(directory, asset.fileName)
        if (destination.isFile && sha256(destination) == asset.sha256) return destination
        if (destination.exists() && !destination.delete()) {
            error("Não foi possível substituir um modelo inválido.")
        }
        val partial = File(directory, "${asset.fileName}.part")
        partial.delete()
        try {
            context.assets.open(asset.path).use { input ->
                FileOutputStream(partial).use { output ->
                    input.copyTo(output, 256 * 1024)
                    output.fd.sync()
                }
            }
            if (sha256(partial) != asset.sha256) error("O modelo ${asset.fileName} está corrompido.")
            if (!partial.renameTo(destination)) error("Não foi possível ativar o modelo verificado.")
            return destination
        } catch (error: Throwable) {
            partial.delete()
            throw error
        }
    }

    private fun sha256(file: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().buffered(256 * 1024).use { input ->
            val buffer = ByteArray(256 * 1024)
            while (true) {
                val read = input.read(buffer)
                if (read < 0) break
                digest.update(buffer, 0, read)
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }
}
