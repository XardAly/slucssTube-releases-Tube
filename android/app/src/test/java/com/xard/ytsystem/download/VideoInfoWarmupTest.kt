package com.xard.ytsystem.download

import kotlinx.coroutines.async
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class VideoInfoWarmupTest {
    @Test
    fun somenteUmaAnaliseViraProprietariaEAOutraReutiliza() = runBlocking {
        val url = "https://example.com/video-unico"
        val first = VideoInfoWarmup.begin(url)
        val second = VideoInfoWarmup.begin(url)

        assertTrue(first.isOwner)
        assertFalse(second.isOwner)
        val waiting = async { second.await(1_000) }
        first.finish()

        assertTrue(waiting.await())
        assertEquals(0, VideoInfoWarmup.activeCountForTesting())
    }

    @Test
    fun esperaDoServicoETerminadaSemReterEntrada() = runBlocking {
        val url = "https://example.com/preparando-download"
        val owner = VideoInfoWarmup.begin(url)
        val waiting = async { VideoInfoWarmup.await(url, 1_000) }
        owner.finish()

        assertTrue(waiting.await())
        assertTrue(VideoInfoWarmup.await(url, 10))
        assertEquals(0, VideoInfoWarmup.activeCountForTesting())
    }

    @Test
    fun timeoutNaoRemoveTrabalhoQueAindaEstaAtivo() = runBlocking {
        val url = "https://example.com/lento"
        val owner = VideoInfoWarmup.begin(url)

        assertFalse(VideoInfoWarmup.await(url, 1))
        assertEquals(1, VideoInfoWarmup.activeCountForTesting())
        owner.finish()
        assertEquals(0, VideoInfoWarmup.activeCountForTesting())
    }
}
