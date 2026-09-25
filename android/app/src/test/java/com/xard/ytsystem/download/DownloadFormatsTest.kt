package com.xard.ytsystem.download

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class DownloadFormatsTest {
    @Test
    fun videoComAudioPrefereAvcM4aSemReduzirAlturaSolicitada() {
        val selector = DownloadFormats.formatSelector("va", 1080)
        assertTrue(selector.startsWith("bestvideo[vcodec^=avc1][height<=1080]+bestaudio[ext=m4a]"))
        assertTrue(selector.contains("bestvideo[height<=1080]+bestaudio"))
    }

    @Test
    fun todoModoTerminaEmStreamCombinado() {
        // TikTok não publica faixa de vídeo separada: sem o fallback para `best`
        // o yt-dlp falha com "Requested format is not available".
        for (mode in listOf("va", "v", "a")) {
            for (height in listOf(null, 720)) {
                val selector = DownloadFormats.formatSelector(mode, height)
                assertTrue(
                    "modo=$mode altura=$height não termina em best: $selector",
                    selector.split("/").last() == "best",
                )
            }
        }
    }

    @Test
    fun apenasVideoPreferemFaixaSeparadaAntesDoCombinado() {
        val selector = DownloadFormats.formatSelector("v", 720)
        val opcoes = selector.split("/")
        assertTrue(opcoes.first() == "bestvideo[vcodec^=avc1][height<=720]")
        assertTrue(opcoes.indexOf("bestvideo[height<=720]") < opcoes.indexOf("best[height<=720]"))
    }

    @Test
    fun audioNaoIncluiFiltroDeVideo() {
        val selector = DownloadFormats.formatSelector("a", 1080)
        assertTrue(selector == "bestaudio/best")
        assertFalse(selector.contains("height"))
    }
}
