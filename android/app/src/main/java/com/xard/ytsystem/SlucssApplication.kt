package com.xard.ytsystem

import android.app.Application
import com.xard.ytsystem.api.ApiClient
import com.xard.ytsystem.data.SlucssDatabase
import com.xard.ytsystem.update.UpdateCheckJob
import java.io.File
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.launch

class SlucssApplication : Application() {
    val database by lazy { SlucssDatabase.get(this) }
    val api by lazy { ApiClient(this) }
    val downloadEvents by lazy { com.xard.ytsystem.identity.DownloadEvents(this) { api.reportDownloadEvent(it) } }
    private val reportQueue = Channel<suspend () -> Unit>(32)
    private val reportScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    fun reportInBackground(block: suspend () -> Unit) {
        reportQueue.trySend(block)
    }

    override fun onCreate() {
        super.onCreate()
        reportScope.launch {
            for (report in reportQueue) {
                try { report() }
                catch (cancelled: CancellationException) { throw cancelled }
                catch (_: Exception) { /* Progress reporting is best effort. */ }
            }
        }
        cleanupInstalledUpdate()
        UpdateCheckJob.schedule(this)
    }

    /** Remove apenas o APK temporário que já foi copiado pelo instalador. */
    private fun cleanupInstalledUpdate() {
        val directory = File(cacheDir, "updates")
        directory.listFiles().orEmpty()
            .filter { it.isFile && shouldDeleteUpdateArtifact(it.name) }
            .forEach(File::delete)
        directory.delete()
    }

    companion object {
        internal fun shouldDeleteUpdateArtifact(name: String): Boolean =
            name == "Slucss-System.apk" ||
                name == "Slucss-System.apk.part" ||
                (name.startsWith("Slucss-System-") && name.endsWith(".apk.part"))
    }
}
