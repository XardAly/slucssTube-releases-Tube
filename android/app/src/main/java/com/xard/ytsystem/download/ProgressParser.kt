package com.xard.ytsystem.download

data class ParsedProgress(
    val percent: Float,
    val downloadedBytes: Long,
    val totalBytes: Long,
    val speedBytes: Long,
    val etaSeconds: Long,
    val stage: String,
)

object ProgressParser {
    private val bytesPattern = Regex("(?i)([0-9.]+)(KiB|MiB|GiB) of ~?([0-9.]+)(KiB|MiB|GiB)")
    private val totalPattern = Regex("(?i)of ~?([0-9.]+)(KiB|MiB|GiB)")
    private val speedPattern = Regex("""(?i)at\s+([0-9.]+)(KiB|MiB|GiB)/s""")

    fun parse(line: String?, fallbackPercent: Float, fallbackEta: Long): ParsedProgress {
        val safe = line.orEmpty().take(512)
        val bytes = bytesPattern.find(safe)
        val totalOnly = totalPattern.find(safe)
        val speed = speedPattern.find(safe)
        val totalBytes = bytes?.let { toBytes(it.groupValues[3], it.groupValues[4]) }
            ?: totalOnly?.let { toBytes(it.groupValues[1], it.groupValues[2]) }
            ?: 0
        val downloadedBytes = bytes?.let { toBytes(it.groupValues[1], it.groupValues[2]) }
            ?: if (totalBytes > 0 && fallbackPercent >= 0) {
                (totalBytes * fallbackPercent.coerceIn(0f, 100f) / 100f).toLong()
            } else 0
        val stage = when {
            safe.contains("Merging formats", ignoreCase = true) -> "merging"
            safe.contains("ExtractAudio", ignoreCase = true) -> "processing"
            safe.contains("VideoConvertor", ignoreCase = true) -> "processing"
            else -> "downloading"
        }
        return ParsedProgress(
            percent = fallbackPercent.coerceIn(0f, 99f),
            downloadedBytes = downloadedBytes,
            totalBytes = totalBytes,
            speedBytes = speed?.let { toBytes(it.groupValues[1], it.groupValues[2]) } ?: 0,
            etaSeconds = fallbackEta,
            stage = stage,
        )
    }

    private fun toBytes(value: String, unit: String): Long {
        val multiplier = when (unit.lowercase()) {
            "gib" -> 1024.0 * 1024 * 1024
            "mib" -> 1024.0 * 1024
            else -> 1024.0
        }
        return ((value.toDoubleOrNull() ?: 0.0) * multiplier).toLong()
    }
}
