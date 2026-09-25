package com.xard.ytsystem.download

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class VideoInfoCacheTest {
    @get:Rule
    val temporaryFolder = TemporaryFolder()

    @Test
    fun hitSobreviveARecriacaoDaClasse() {
        val root = temporaryFolder.newFolder("cache")
        val url = "https://youtu.be/video"
        val first = VideoInfoCache(root)
        val staging = first.createStaging(url)
        staging.infoFile.writeText("{\"id\":\"video\"}")
        assertTrue(first.commit(url, staging)?.isFile == true)

        val destination = File(temporaryFolder.newFolder("job"), "analysis.info.json")
        val copied = VideoInfoCache(root).copyFreshTo(url, destination)

        assertEquals("{\"id\":\"video\"}", copied?.readText())
    }

    @Test
    fun entradaExpiraNoLimiteDoTtl() {
        var now = 1_000L
        val root = temporaryFolder.newFolder("cache")
        val cache = VideoInfoCache(root, nowMillis = { now }, ttlMillis = 100L)
        val staging = cache.createStaging("https://example.com/video")
        staging.infoFile.writeText("{}")
        cache.commit("https://example.com/video", staging)

        now += 100L

        assertNull(
            cache.copyFreshTo(
                "https://example.com/video",
                File(temporaryFolder.newFolder("expired-job"), "analysis.info.json"),
            ),
        )
        assertTrue(root.listFiles().orEmpty().none { !it.name.startsWith('.') })
    }

    @Test
    fun rejeitaArquivoVazioEOversized() {
        val root = temporaryFolder.newFolder("cache")
        val cache = VideoInfoCache(root, maxEntryBytes = 4L)

        val empty = cache.createStaging("https://example.com/empty")
        empty.infoFile.writeBytes(byteArrayOf())
        assertNull(cache.readyForParsing(empty))
        assertNull(cache.commit("https://example.com/empty", empty))

        val oversized = cache.createStaging("https://example.com/large")
        oversized.infoFile.writeText("12345")
        assertNull(cache.readyForParsing(oversized))
        assertNull(cache.commit("https://example.com/large", oversized))
        assertTrue(root.listFiles().orEmpty().none { it.name.length == 64 + ".info.json".length })
    }

    @Test
    fun limitaQuantidadeDeEntradasSemCacheEmMemoria() {
        var now = 10_000L
        val root = temporaryFolder.newFolder("cache")
        val cache = VideoInfoCache(root, nowMillis = { now }, maxEntries = 2)
        repeat(3) { index ->
            val url = "https://example.com/$index"
            val staging = cache.createStaging(url)
            staging.infoFile.writeText("{\"id\":$index}")
            cache.commit(url, staging)
            now += 1_000L
        }

        val cachedFiles = root.listFiles().orEmpty().filter { !it.name.startsWith('.') }
        assertEquals(2, cachedFiles.size)
        assertNull(
            cache.copyFreshTo(
                "https://example.com/0",
                File(temporaryFolder.newFolder("old-job"), "analysis.info.json"),
            ),
        )
    }

    @Test
    fun stagingNaoViraHitAntesDoCommitEPodeSerDescartado() {
        val root = temporaryFolder.newFolder("cache")
        val cache = VideoInfoCache(root)
        val url = "https://example.com/pending"
        val staging = cache.createStaging(url)
        staging.infoFile.writeText("{}")

        assertNull(
            cache.copyFreshTo(
                url,
                File(temporaryFolder.newFolder("pending-job"), "analysis.info.json"),
            ),
        )
        cache.discard(staging)
        assertFalse(staging.infoFile.exists())
    }
}
