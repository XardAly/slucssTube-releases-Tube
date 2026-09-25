package com.xard.ytsystem

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.xard.ytsystem.identity.DownloadEvents
import com.xard.ytsystem.data.DownloadJob
import com.google.gson.JsonObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

@RunWith(AndroidJUnit4::class)
class DownloadEventsTest {
    @Test fun failedSenderDoesNotBlockProducerOrPreventFollowingEvents() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val release = CountDownLatch(1)
        val finished = CountDownLatch(1)
        val received = java.util.Collections.synchronizedList(mutableListOf<JsonObject>())
        val events = DownloadEvents(context) { event ->
            received.add(event)
            if (event["event"].asString == "download_started") {
                release.await(5, TimeUnit.SECONDS)
                throw java.io.IOException("offline")
            }
            finished.countDown()
        }
        val job = DownloadJob(id = "test", type = DownloadJob.TYPE_DOWNLOAD, title = "Vídeo", height = 1080)
        // Both calls must return even though the consumer is deliberately blocked.
        events.emit(job, "started", "João")
        events.emit(job, "completed", "João", fileName = "video.mp4")
        release.countDown()
        assertTrue(finished.await(5, TimeUnit.SECONDS))
        assertEquals(2, received.size)
        assertEquals("João", received[1].getAsJsonObject("user")["name"].asString)
        assertEquals("1080p", received[1].getAsJsonObject("download")["quality"].asString)
        assertEquals(setOf("manufacturer", "model", "android_version"), received[1].getAsJsonObject("device").keySet())
    }
}
