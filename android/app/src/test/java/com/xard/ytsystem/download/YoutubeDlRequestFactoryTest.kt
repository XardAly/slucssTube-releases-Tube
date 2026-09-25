package com.xard.ytsystem.download

import com.xard.ytsystem.data.DownloadJob
import com.xard.ytsystem.LocalTools
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class YoutubeDlRequestFactoryTest {
    private val job = DownloadJob(
        id = "job-1",
        type = DownloadJob.TYPE_DOWNLOAD,
        url = "https://example.com/video",
        title = "Vídeo",
        mode = "va",
        height = 720,
        videoFormat = "mp4",
    )

    @Test
    fun cacheUsaLoadInfoJsonSemReextrairUrl() {
        val command = YoutubeDlRequestFactory.download(
            job = job,
            workDir = File("work"),
            jsRuntime = "quickjs:/libqjs.so",
            cookies = null,
            cachedInfo = File("analysis.info.json"),
        ).buildCommand()

        assertTrue(command.contains("--load-info-json"))
        assertTrue(command.any { it.endsWith("analysis.info.json") })
        assertFalse(command.contains(requireNotNull(job.url)))
        assertTrue(command.contains("-f"))
        assertTrue(command.contains("--merge-output-format"))
    }

    @Test
    fun cacheAusentePreservaUrlECookies() {
        val command = YoutubeDlRequestFactory.download(
            job = job,
            workDir = File("work"),
            jsRuntime = "quickjs:/libqjs.so",
            cookies = "cookies.txt",
            cachedInfo = null,
        ).buildCommand()

        assertTrue(command.contains(requireNotNull(job.url)))
        assertFalse(command.contains("--load-info-json"))
        assertTrue(command.contains("--cookies"))
        assertTrue(command.contains("cookies.txt"))
    }

    @Test
    fun tiktokRecebeRefererSemAfetarOutrasPlataformas() {
        val tiktokJob = job.copy(url = "https://vt.tiktok.com/ZSVpML8wd/")
        val tiktokCommand = YoutubeDlRequestFactory.download(
            job = tiktokJob,
            workDir = File("work"),
            jsRuntime = "quickjs:/libqjs.so",
            cookies = null,
            cachedInfo = null,
        ).buildCommand()
        val refererIndex = tiktokCommand.indexOf("--referer")

        assertTrue(refererIndex >= 0)
        assertEquals(LocalTools.TIKTOK_REFERER, tiktokCommand[refererIndex + 1])

        val otherCommand = YoutubeDlRequestFactory.download(
            job = job,
            workDir = File("work"),
            jsRuntime = "quickjs:/libqjs.so",
            cookies = null,
            cachedInfo = null,
        ).buildCommand()
        assertFalse(otherCommand.contains("--referer"))
    }
}
