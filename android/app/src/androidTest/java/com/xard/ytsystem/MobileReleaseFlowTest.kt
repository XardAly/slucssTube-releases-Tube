package com.xard.ytsystem

import android.Manifest
import android.app.NotificationManager
import android.content.Context
import android.media.MediaPlayer
import android.os.Build
import android.provider.MediaStore
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.xard.ytsystem.api.AndroidManifest
import com.xard.ytsystem.download.MediaPublisher
import com.xard.ytsystem.update.ReleaseNotifier
import com.xard.ytsystem.update.RemoteConfigStore
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

@RunWith(AndroidJUnit4::class)
class MobileReleaseFlowTest {
    private val context = ApplicationProvider.getApplicationContext<Context>()
    private fun manifest(code: Int, policy: String = "recommended") = AndroidManifest(
        "1.3.0", code, 1, "https://example.invalid/app.apk", "a".repeat(64),
        listOf("Teste"), "local", false, true, 20, "test", "test", "test",
        "test-release-$code", "2026-09-25T00:00:00Z", policy,
    )

    @Test fun publishCreatesVisiblePlayableFileAndRejectsEmptySource() = runBlocking {
        val source = File(context.cacheDir, "mobile-release-fixture.mp4")
        InstrumentationRegistry.getInstrumentation().context.assets.open("mobile-release-fixture.mp4").use { input ->
            source.outputStream().use { input.copyTo(it) }
        }
        val media = MediaPublisher.publish(context, source, "teste:/arquivo? inválido")
        try {
            context.contentResolver.query(media.uri, arrayOf(MediaStore.MediaColumns.IS_PENDING,
                MediaStore.MediaColumns.SIZE, MediaStore.MediaColumns.DISPLAY_NAME), null, null, null)!!.use {
                assertTrue(it.moveToFirst()); assertEquals(0, it.getInt(0))
                assertEquals(source.length(), it.getLong(1))
                assertFalse(it.getString(2).contains("?"))
            }
            MediaPlayer().apply {
                try { setDataSource(context, media.uri); prepare(); assertTrue(duration >= 1000); start(); assertTrue(isPlaying) }
                finally { release() }
            }
        } finally { context.contentResolver.delete(media.uri, null, null); source.delete() }
        source.writeBytes(byteArrayOf())
        try {
            try { MediaPublisher.publish(context, source, "empty.mp4"); fail("Empty media accepted") }
            catch (_: java.io.IOException) { }
        } finally { source.delete() }
    }

    @Test fun notificationIsPersistentDeduplicatedAndOpensUpdate() {
        if (Build.VERSION.SDK_INT >= 33) {
            InstrumentationRegistry.getInstrumentation().uiAutomation
                .grantRuntimePermission(context.packageName, Manifest.permission.POST_NOTIFICATIONS)
        }
        val prefs = context.getSharedPreferences("update_check", Context.MODE_PRIVATE)
        prefs.edit().clear().commit()
        val manager = context.getSystemService(NotificationManager::class.java)
        val release = manifest(BuildConfig.VERSION_CODE + 1)
        ReleaseNotifier.notify(context, release)
        val first = manager.activeNotifications.single { it.id == 4103 }
        assertNotNull(first.notification.contentIntent)
        assertEquals(release.releaseId, prefs.getString("release_notified", ""))
        manager.cancel(4103)
        ReleaseNotifier.notify(context, release)
        assertFalse(manager.activeNotifications.any { it.id == 4103 })
        ReleaseNotifier.notify(context, manifest(BuildConfig.VERSION_CODE + 2))
        assertTrue(manager.activeNotifications.any { it.id == 4103 })
        manager.cancel(4103)
        prefs.edit().clear().commit()
    }

    @Test fun cancelWhileSavingRemovesPendingMedia() = runBlocking {
        val name = "cancel-${java.util.UUID.randomUUID()}.mp4"
        val source = File(context.cacheDir, name).apply { writeBytes(ByteArray(1024)) }
        try {
            try { MediaPublisher.publish(context, source, name) { true }; fail("Cancellation ignored") }
            catch (_: java.io.InterruptedIOException) { }
            context.contentResolver.query(MediaStore.Downloads.EXTERNAL_CONTENT_URI,
                arrayOf(MediaStore.MediaColumns._ID), "${MediaStore.MediaColumns.DISPLAY_NAME} = ?",
                arrayOf(name), null)!!.use { assertEquals(0, it.count) }
        } finally { source.delete() }
    }

    @Test fun mandatoryPolicySurvivesStoreRecreationAndUnlocksAfterNewManifest() {
        val store = RemoteConfigStore(context)
        try {
            store.update(manifest(BuildConfig.VERSION_CODE + 1, "mandatory"))
            assertTrue(RemoteConfigStore(context).requiresUpdate)
            store.update(manifest(BuildConfig.VERSION_CODE, "mandatory"))
            assertFalse(RemoteConfigStore(context).requiresUpdate)
        } finally { context.getSharedPreferences("remote_config", Context.MODE_PRIVATE).edit().clear().commit() }
    }

    @Test fun bundledEnginesInitializeWithoutDeveloperFiles() {
        LocalTools.ensureInitialized(context)
        for (name in listOf("libpython.so", "libffmpeg.so", "libqjs.so")) {
            assertTrue(File(context.applicationInfo.nativeLibraryDir, name).isFile)
        }
        val root = File(context.noBackupFilesDir, "youtubedl-android")
        assertTrue(File(root, "packages/python/usr/etc/tls/cert.pem").length() > 0)
        assertTrue(File(root, "yt-dlp/yt-dlp").length() > 0)
        // Native bridge does not translate subprocess execve. Execution requires
        // a real ARM system; never treat an x86 emulator as ARM download proof.
        org.junit.Assume.assumeFalse(Build.SUPPORTED_ABIS.first().startsWith("x86"))
        val process = ProcessBuilder(LocalTools.ffmpegExecutable(context), "-version")
            .redirectErrorStream(true).apply {
                environment()["LD_LIBRARY_PATH"] = LocalTools.ffmpegLibraryPath(context)
            }.start()
        val output = process.inputStream.bufferedReader().readText()
        assertEquals(0, process.waitFor())
        assertTrue(output.contains("ffmpeg version"))
    }
}
