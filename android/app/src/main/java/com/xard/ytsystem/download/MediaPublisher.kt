package com.xard.ytsystem.download

import android.content.ContentValues
import android.content.Context
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import androidx.core.content.FileProvider
import androidx.annotation.RequiresApi
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.IOException
import android.media.MediaScannerConnection
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.currentCoroutineContext

data class PublishedMedia(val uri: Uri, val name: String, val mime: String)

object MediaPublisher {
    suspend fun publish(context: Context, source: File, requestedName: String,
                        shouldCancel: () -> Boolean = { false }): PublishedMedia =
        withContext(Dispatchers.IO) {
            val extension = source.extension.lowercase()
            if (!source.isFile || source.length() == 0L) throw IOException("Arquivo de origem vazio ou ausente.")
            val mime = DownloadFormats.mimeFor(extension)
            val safeName = sanitize(requestedName.ifBlank { source.name }, extension)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                publishMediaStore(context, source, safeName, mime, shouldCancel)
            } else {
                publishLegacy(context, source, safeName, mime, shouldCancel)
            }
        }

    @RequiresApi(Build.VERSION_CODES.Q)
    private suspend fun publishMediaStore(
        context: Context,
        source: File,
        name: String,
        mime: String,
        shouldCancel: () -> Boolean,
    ): PublishedMedia {
        val resolver = context.contentResolver
        // A coleção Downloads aceita todos os MIME types e permite a pasta
        // Download/Slucss. Coleções Video/Audio restringem o diretório raiz
        // a Movies/Music em alguns fabricantes.
        val collection = MediaStore.Downloads.EXTERNAL_CONTENT_URI
        val values = ContentValues().apply {
            put(MediaStore.MediaColumns.DISPLAY_NAME, name)
            put(MediaStore.MediaColumns.MIME_TYPE, mime)
            put(MediaStore.MediaColumns.RELATIVE_PATH, "Download/Slucss")
            put(MediaStore.MediaColumns.IS_PENDING, 1)
        }
        val uri = resolver.insert(collection, values)
            ?: throw IllegalStateException("O Android recusou criar o arquivo final.")
        try {
            resolver.openOutputStream(uri, "w")?.use { output ->
                FileInputStream(source).use { input -> copyChecked(input, output, shouldCancel) }
            } ?: throw IllegalStateException("Não foi possível abrir o destino.")
            if (resolver.update(uri, ContentValues().apply {
                put(MediaStore.MediaColumns.IS_PENDING, 0)
            }, null, null) != 1) throw IOException("O Android não publicou o arquivo final.")
            return PublishedMedia(uri, name, mime)
        } catch (error: Exception) {
            runCatching { resolver.delete(uri, null, null) }
            throw error
        }
    }

    @Suppress("DEPRECATION")
    private suspend fun publishLegacy(
        context: Context,
        source: File,
        name: String,
        mime: String,
        shouldCancel: () -> Boolean,
    ): PublishedMedia {
        val directory = File(
            Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS),
            "Slucss",
        ).apply { if (!isDirectory && !mkdirs()) throw IOException("Falha ao criar a pasta Downloads/Slucss.") }
        val destination = uniqueFile(directory, name)
        try {
        FileInputStream(source).use { input ->
            FileOutputStream(destination).use { output -> copyChecked(input, output, shouldCancel); output.fd.sync() }
        }
        val uri = FileProvider.getUriForFile(context, "${context.packageName}.files", destination)
        MediaScannerConnection.scanFile(context, arrayOf(destination.absolutePath), arrayOf(mime), null)
        return PublishedMedia(uri, destination.name, mime)
        } catch (error: Exception) {
            destination.delete()
            throw error
        }
    }

    private suspend fun copyChecked(input: java.io.InputStream, output: java.io.OutputStream,
                                    shouldCancel: () -> Boolean) {
        val buffer = ByteArray(COPY_BUFFER)
        while (true) {
            currentCoroutineContext().ensureActive()
            if (shouldCancel()) throw java.io.InterruptedIOException("Publicação cancelada.")
            val read = input.read(buffer)
            if (read < 0) break
            output.write(buffer, 0, read)
        }
    }

    private fun sanitize(name: String, extension: String): String {
        val cleaned = name.replace(Regex("[\\/:*?\"<>|\\u0000-\\u001f]"), "_")
            .trim().trim('.').take(160).ifBlank { "Slucss-${System.currentTimeMillis()}" }
        return if (cleaned.substringAfterLast('.', "").equals(extension, true)) cleaned
        else "$cleaned.$extension"
    }

    private fun uniqueFile(directory: File, name: String): File {
        val first = File(directory, name)
        if (!first.exists()) return first
        val stem = name.substringBeforeLast('.', name)
        val extension = name.substringAfterLast('.', "")
        var number = 2
        while (true) {
            val candidate = File(directory, "$stem ($number)${if (extension.isBlank()) "" else ".$extension"}")
            if (!candidate.exists()) return candidate
            number++
        }
    }

    private const val COPY_BUFFER = 256 * 1024
}
