package com.xard.ytsystem.data

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import kotlinx.coroutines.flow.Flow

@Dao
interface DownloadJobDao {
    @Query("SELECT * FROM download_jobs ORDER BY createdAt DESC")
    fun observeAll(): Flow<List<DownloadJob>>

    @Query("SELECT * FROM download_jobs WHERE id = :id LIMIT 1")
    suspend fun get(id: String): DownloadJob?

    @Query("SELECT * FROM download_jobs WHERE status = 'queued' ORDER BY createdAt ASC LIMIT 1")
    suspend fun nextQueued(): DownloadJob?

    @Query("SELECT COUNT(*) FROM download_jobs WHERE status IN ('queued', 'running')")
    suspend fun countActive(): Int

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsert(job: DownloadJob)

    @Query("UPDATE download_jobs SET operationId = :operationId, updatedAt = :now WHERE id = :id")
    suspend fun setOperationId(
        id: String,
        operationId: String,
        now: Long = System.currentTimeMillis(),
    )

    @Query("UPDATE download_jobs SET status = 'queued', stage = 'queued', message = 'Retomando tarefa interrompida.', updatedAt = :now WHERE status = 'running'")
    suspend fun recoverInterrupted(now: Long = System.currentTimeMillis())

    @Query("UPDATE download_jobs SET status = :status, stage = :stage, progress = :progress, downloadedBytes = :downloaded, totalBytes = :total, speedBytes = :speed, etaSeconds = :eta, message = :message, updatedAt = :now WHERE id = :id AND status NOT IN ('completed', 'failed', 'cancelled')")
    suspend fun updateProgress(
        id: String,
        status: String,
        stage: String,
        progress: Float,
        downloaded: Long,
        total: Long,
        speed: Long,
        eta: Long,
        message: String,
        now: Long = System.currentTimeMillis(),
    )

    @Query("UPDATE download_jobs SET status = :status, stage = :stage, progress = :progress, message = :message, outputUri = :outputUri, outputMime = :outputMime, outputName = :outputName, error = :error, errorDetail = :errorDetail, fallbackEligible = :fallbackEligible, updatedAt = :now WHERE id = :id")
    suspend fun finish(
        id: String,
        status: String,
        stage: String,
        progress: Float,
        message: String,
        outputUri: String? = null,
        outputMime: String? = null,
        outputName: String? = null,
        error: String? = null,
        errorDetail: String? = null,
        fallbackEligible: Boolean = false,
        now: Long = System.currentTimeMillis(),
    )

    @Query("DELETE FROM download_jobs WHERE status IN ('completed', 'failed', 'cancelled')")
    suspend fun clearFinished()
}
