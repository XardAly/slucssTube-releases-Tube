package com.xard.ytsystem.download

import com.xard.ytsystem.LocalTools
import com.xard.ytsystem.data.DownloadJob
import com.yausername.youtubedl_android.YoutubeDLRequest
import java.io.File

internal object YoutubeDlRequestFactory {
    fun download(
        job: DownloadJob,
        workDir: File,
        jsRuntime: String,
        cookies: String?,
        cachedInfo: File?,
    ): YoutubeDLRequest {
        val request = if (cachedInfo == null) {
            YoutubeDLRequest(requireNotNull(job.url))
        } else {
            YoutubeDLRequest(emptyList())
                .addOption("--load-info-json", cachedInfo.absolutePath)
        }

        request
            .addOption("--no-playlist")
            .addOption("--continue")
            .addOption("--no-mtime")
            .addOption("--restrict-filenames")
            .addOption("--newline")
            .addOption("--progress-delta", 1)
            .addOption("--js-runtimes", jsRuntime)
            .addOption("--user-agent", LocalTools.BROWSER_USER_AGENT)
            .addOption("--add-header", LocalTools.ACCEPT_LANGUAGE_HEADER)
            .addOption("--extractor-args", LocalTools.YOUTUBE_EXTRACTOR_ARGS)
            .addOption("--socket-timeout", 20)
            .addOption("--retries", 5)
            .addOption("--fragment-retries", 5)
            .addOption("-o", File(workDir, "%(title).180B [%(id)s].%(ext)s").absolutePath)
            .addOption("-f", DownloadFormats.formatSelector(job.mode, job.height))

        if (job.url?.let(LocalTools::isTikTokLink) == true) {
            request.addOption("--referer", LocalTools.TIKTOK_REFERER)
        }

        if (cookies != null) request.addOption("--cookies", cookies)

        if (job.mode == "a") {
            request.addOption("--extract-audio")
                .addOption("--audio-format", job.audioFormat)
                .addOption("--audio-quality", "192K")
        } else {
            request.addOption("--merge-output-format", job.videoFormat)
        }
        return request
    }
}
