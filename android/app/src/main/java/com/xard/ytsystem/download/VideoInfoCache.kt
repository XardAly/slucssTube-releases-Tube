package com.xard.ytsystem.download

import android.content.Context
import java.io.File
import java.nio.file.AtomicMoveNotSupportedException
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.security.MessageDigest
import java.util.UUID

/**
 * Cache curto do info JSON produzido pelo yt-dlp durante a análise.
 *
 * O arquivo contém URLs temporárias da plataforma, por isso fica no diretório
 * privado sem backup, nunca é mantido em memória e expira rapidamente.
 */
internal class VideoInfoCache(
    private val directory: File,
    private val nowMillis: () -> Long = System::currentTimeMillis,
    private val ttlMillis: Long = DEFAULT_TTL_MILLIS,
    private val maxEntries: Int = DEFAULT_MAX_ENTRIES,
    private val maxEntryBytes: Long = DEFAULT_MAX_ENTRY_BYTES,
) {
    internal class Staging internal constructor(val outputBase: File) {
        val infoFile: File = File(outputBase.parentFile, outputBase.name + INFO_SUFFIX)
        val outputTemplate: String = "infojson:${outputBase.absolutePath}"
    }

    fun createStaging(url: String): Staging = synchronized(FILE_LOCK) {
        check(directory.isDirectory || directory.mkdirs()) {
            "Não foi possível preparar o cache de análise"
        }
        cleanupLocked()
        Staging(
            File(
                directory,
                ".${key(url)}.${UUID.randomUUID()}.stage",
            ),
        )
    }

    /** Impede que um JSON anormalmente grande seja carregado inteiro na RAM. */
    fun readyForParsing(staging: Staging): File? = synchronized(FILE_LOCK) {
        staging.infoFile.takeIf(::isValidSize)
    }

    /** Promove para o cache apenas um info JSON completo e dentro do limite. */
    fun commit(url: String, staging: Staging): File? = synchronized(FILE_LOCK) {
        val source = staging.infoFile
        if (!isValidSize(source)) {
            source.delete()
            return@synchronized null
        }
        val target = cacheFile(url)
        try {
            try {
                Files.move(
                    source.toPath(),
                    target.toPath(),
                    StandardCopyOption.ATOMIC_MOVE,
                    StandardCopyOption.REPLACE_EXISTING,
                )
            } catch (_: AtomicMoveNotSupportedException) {
                Files.move(
                    source.toPath(),
                    target.toPath(),
                    StandardCopyOption.REPLACE_EXISTING,
                )
            }
            target.setLastModified(nowMillis())
            cleanupLocked(target)
            target.takeIf(::isFreshLocked)
        } catch (_: Exception) {
            source.delete()
            null
        }
    }

    /**
     * Copia um hit para o diretório exclusivo da tarefa. A cópia evita que uma
     * limpeza concorrente remova o arquivo enquanto o yt-dlp ainda o lê.
     */
    fun copyFreshTo(url: String, destination: File): File? = synchronized(FILE_LOCK) {
        cleanupLocked()
        val source = cacheFile(url)
        if (!isFreshLocked(source)) {
            source.delete()
            return@synchronized null
        }
        try {
            val parent = destination.parentFile
            check(parent == null || parent.isDirectory || parent.mkdirs())
            source.copyTo(destination, overwrite = true)
            destination.takeIf { it.length() == source.length() && isValidSize(it) }
                ?: run {
                    destination.delete()
                    null
                }
        } catch (_: Exception) {
            destination.delete()
            null
        }
    }

    fun invalidate(url: String) = synchronized(FILE_LOCK) {
        cacheFile(url).delete()
    }

    fun discard(staging: Staging) = synchronized(FILE_LOCK) {
        staging.infoFile.delete()
        staging.outputBase.delete()
    }

    private fun cleanupLocked(keep: File? = null) {
        if (!directory.isDirectory) return
        val now = nowMillis()
        directory.listFiles().orEmpty().forEach { file ->
            when {
                isStagingFile(file) -> {
                    val age = now - file.lastModified()
                    if (age !in 0 until STAGING_TTL_MILLIS) file.delete()
                }
                isCacheFile(file) && !isFreshLocked(file) -> file.delete()
            }
        }
        val cached = directory.listFiles().orEmpty().filter(::isCacheFile)
        val excess = cached.size - maxEntries.coerceAtLeast(1)
        if (excess > 0) {
            cached.asSequence()
                .filter { it != keep }
                .sortedBy(File::lastModified)
                .take(excess)
                .forEach(File::delete)
        }
    }

    private fun isFreshLocked(file: File): Boolean {
        if (!isValidSize(file)) return false
        val age = nowMillis() - file.lastModified()
        return age in 0 until ttlMillis
    }

    private fun isValidSize(file: File): Boolean =
        file.isFile && file.length() in 1..maxEntryBytes

    private fun isCacheFile(file: File): Boolean {
        val hash = file.name.removeSuffix(INFO_SUFFIX)
        return file.isFile && file.name.endsWith(INFO_SUFFIX) &&
            hash.length == SHA_256_HEX_LENGTH && hash.all { it in HEX_CHARS }
    }

    private fun isStagingFile(file: File): Boolean =
        file.isFile && file.name.startsWith('.') && file.name.endsWith(STAGING_SUFFIX)

    private fun cacheFile(url: String): File = File(directory, key(url) + INFO_SUFFIX)

    private fun key(url: String): String = MessageDigest.getInstance("SHA-256")
        .digest(url.toByteArray(Charsets.UTF_8))
        .joinToString("") { "%02x".format(it) }

    companion object {
        private const val DIRECTORY_NAME = "yt-dlp-info-cache"
        private const val INFO_SUFFIX = ".info.json"
        private const val STAGING_SUFFIX = ".stage.info.json"
        private const val SHA_256_HEX_LENGTH = 64
        private const val HEX_CHARS = "0123456789abcdef"
        private const val STAGING_TTL_MILLIS = 2L * 60 * 1000
        internal const val DEFAULT_TTL_MILLIS = 10L * 60 * 1000
        internal const val DEFAULT_MAX_ENTRIES = 6
        internal const val DEFAULT_MAX_ENTRY_BYTES = 8L * 1024 * 1024
        private val FILE_LOCK = Any()

        fun from(context: Context): VideoInfoCache = VideoInfoCache(
            File(context.applicationContext.noBackupFilesDir, DIRECTORY_NAME),
        )
    }
}
