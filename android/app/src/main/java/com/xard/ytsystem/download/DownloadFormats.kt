package com.xard.ytsystem.download

object DownloadFormats {
    fun formatSelector(mode: String, height: Int?): String {
        val limit = height?.let { "[height<=$it]" }.orEmpty()
        return when (mode) {
            "a" -> "bestaudio/best"
            // O fallback para stream combinado é obrigatório: plataformas como o
            // TikTok não publicam faixa de vídeo separada, e sem ele o yt-dlp
            // responde "Requested format is not available". Espelha o
            // build_format_spec do servidor, que sempre termina em `best`.
            "v" -> "bestvideo[vcodec^=avc1]$limit/bestvideo$limit/best$limit/bestvideo/best"
            else ->
                "bestvideo[vcodec^=avc1]$limit+bestaudio[ext=m4a]/" +
                    "bestvideo[vcodec^=avc1]$limit+bestaudio/" +
                    "bestvideo$limit+bestaudio[ext=m4a]/" +
                    "bestvideo$limit+bestaudio/best$limit/best"
        }
    }

    fun mimeFor(extension: String): String = when (extension.lowercase()) {
        "mp4", "m4v" -> "video/mp4"
        "mkv" -> "video/x-matroska"
        "webm" -> "video/webm"
        "mp3" -> "audio/mpeg"
        "m4a" -> "audio/mp4"
        "opus" -> "audio/ogg"
        "gif" -> "image/gif"
        "jpg", "jpeg" -> "image/jpeg"
        "png" -> "image/png"
        else -> "application/octet-stream"
    }
}
