package com.xard.ytsystem.upscale

import android.media.MediaCodec
import android.media.MediaExtractor
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class UpscaleContractTest {
    @Test
    fun `flags do extractor sao convertidas sem confundir partial frame com eos`() {
        val source = MediaExtractor.SAMPLE_FLAG_SYNC or
            MediaExtractor.SAMPLE_FLAG_PARTIAL_FRAME or
            MediaExtractor.SAMPLE_FLAG_ENCRYPTED

        assertEquals(
            MediaCodec.BUFFER_FLAG_KEY_FRAME or MediaCodec.BUFFER_FLAG_PARTIAL_FRAME,
            mediaCodecInputFlags(source),
        )
    }

    @Test
    fun animeVideoV3EPadraoEModelosTemVersao() {
        val options = UpscaleOptions()
        assertEquals(UpscaleModel.ANIME_VIDEO_V3.apiValue, options.model)
        assertEquals(2, options.scale)
        assertEquals("auto", options.outputFormat)
        assertEquals(UpscaleVersions.ENGINE_VERSION, options.engineVersion)
        assertTrue(UpscaleModel.entries.all { it.modelVersion.isNotBlank() })
    }

    @Test
    fun buildIncluiSomenteOsPesosNecessarios() {
        val build = sequenceOf(
            File("app/build.gradle.kts"),
            File("build.gradle.kts"),
        ).firstOrNull(File::isFile)?.readText() ?: error("build.gradle.kts não encontrado")
        for (scale in 2..4) {
            assertTrue(build.contains("realesr-animevideov3-x$scale.param"))
            assertTrue(build.contains("up${scale}x-conservative.bin"))
        }
        assertTrue(build.contains("realesr-animevideov3-x2.bin"))
        assertTrue(!build.contains("realesr-animevideov3-x3.bin"))
        assertTrue(!build.contains("realesr-animevideov3-x4.bin"))
        assertTrue(!build.contains("up2x-denoise"))
        assertTrue(!build.contains("realesrgan-x4plus.bin"))
    }

    @Test
    fun reduzSessentaFpsParaMaiorTaxaInformadaPeloEncoder() {
        val candidates = encoderFrameRateCandidates(60.0, 30.0)

        assertEquals(30.0, candidates.first(), 0.001)
        assertTrue(candidates.none { it > 30.0 })
    }

    @Test
    fun limitadorDescartaFramesUniformementeSemAlterarPtsAceitos() {
        val limiter = FrameRateLimiter(60.0, 30.0)
        val timestamps = listOf(0L, 16_667L, 33_333L, 50_000L, 66_667L)

        assertEquals(
            listOf(0L, 33_333L, 66_667L),
            timestamps.filter(limiter::shouldEncode),
        )
    }

    @Test
    fun limitadorNaoRemoveFramesQuandoEncoderAceitaTaxaOriginal() {
        val limiter = FrameRateLimiter(60.0, 60.0)
        val timestamps = listOf(0L, 16_667L, 33_333L)

        assertEquals(timestamps, timestamps.filter(limiter::shouldEncode))
    }
}
