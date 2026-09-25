package com.xard.ytsystem.ui

import com.xard.ytsystem.data.DownloadJob
import org.junit.Assert.assertEquals
import org.junit.Test

class QueueFilterTest {
    @Test fun filtersKeepWaitingAndRunningTogetherAndCancelledInHistory() {
        val jobs = listOf("queued", "running", "completed", "failed", "cancelled").map {
            DownloadJob(id = it, type = DownloadJob.TYPE_DOWNLOAD, title = it, status = it)
        }
        assertEquals(listOf("queued", "running"), jobs.filter(QueueFilter.ACTIVE::accepts).map { it.id })
        assertEquals(listOf("completed"), jobs.filter(QueueFilter.COMPLETED::accepts).map { it.id })
        assertEquals(listOf("failed"), jobs.filter(QueueFilter.FAILED::accepts).map { it.id })
        assertEquals(jobs, jobs.filter(QueueFilter.ALL::accepts))
    }
}
