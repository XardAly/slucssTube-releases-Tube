package com.xard.ytsystem.ui

import com.xard.ytsystem.data.DownloadJob

enum class QueueFilter {
    ALL, ACTIVE, COMPLETED, FAILED;

    fun accepts(job: DownloadJob): Boolean = when (this) {
        ALL -> true
        ACTIVE -> job.status == DownloadJob.STATUS_RUNNING || job.status == DownloadJob.STATUS_QUEUED
        COMPLETED -> job.status == DownloadJob.STATUS_COMPLETED
        FAILED -> job.status == DownloadJob.STATUS_FAILED
    }
}
