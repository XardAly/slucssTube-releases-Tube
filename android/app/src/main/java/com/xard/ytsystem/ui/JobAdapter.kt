package com.xard.ytsystem.ui

import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import androidx.core.content.ContextCompat
import com.xard.ytsystem.R
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.ListAdapter
import androidx.recyclerview.widget.RecyclerView
import com.xard.ytsystem.data.DownloadJob
import com.xard.ytsystem.databinding.ItemDownloadJobBinding
import java.util.Locale

class JobAdapter(
    private val onCancel: (DownloadJob) -> Unit,
    private val onOpen: (DownloadJob) -> Unit,
    private val onFallback: (DownloadJob) -> Unit,
    private val onShowError: (DownloadJob) -> Unit,
    private val onRetry: (DownloadJob) -> Unit = {},
    private val onShare: (DownloadJob) -> Unit = {},
) : ListAdapter<DownloadJob, JobAdapter.Holder>(Diff) {

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): Holder = Holder(
        ItemDownloadJobBinding.inflate(LayoutInflater.from(parent.context), parent, false),
    )

    override fun onBindViewHolder(holder: Holder, position: Int) = holder.bind(getItem(position))

    inner class Holder(private val binding: ItemDownloadJobBinding) :
        RecyclerView.ViewHolder(binding.root) {
        fun bind(job: DownloadJob) = with(binding) {
            val (typeLabel, icon) = when (job.type) {
                DownloadJob.TYPE_GIF -> "GIF" to R.drawable.ic_nav_gif
                DownloadJob.TYPE_COMPATIBILITY -> "CONVERSÃO" to R.drawable.ic_nav_convert
                DownloadJob.TYPE_UPSCALE -> "UPSCALE IA" to R.drawable.ic_nav_upscale
                else -> "DOWNLOAD" to R.drawable.ic_nav_download
            }
            jobType.text = typeLabel
            jobIcon.setImageResource(icon)
            val stateColor = ContextCompat.getColor(root.context, when (job.status) {
                DownloadJob.STATUS_COMPLETED -> R.color.success
                DownloadJob.STATUS_FAILED -> R.color.red_highlight
                DownloadJob.STATUS_RUNNING -> R.color.red_highlight
                DownloadJob.STATUS_QUEUED -> R.color.warning
                else -> R.color.text_muted
            })
            jobIcon.setColorFilter(stateColor)
            jobState.setTextColor(stateColor)
            jobState.text = when (job.status) {
                DownloadJob.STATUS_COMPLETED -> "CONCLUÍDO"
                DownloadJob.STATUS_RUNNING -> if (job.progress > 0) "${job.progress.toInt().coerceIn(0, 100)}%" else "PROCESSANDO"
                DownloadJob.STATUS_QUEUED -> "NA FILA"
                DownloadJob.STATUS_CANCELLED -> "CANCELADO"
                else -> "FALHOU"
            }
            jobTitle.text = job.title
            jobStatus.text = when (job.status) {
                DownloadJob.STATUS_QUEUED -> "Aguardando na fila"
                DownloadJob.STATUS_RUNNING -> job.message
                DownloadJob.STATUS_COMPLETED -> "Concluído · ${job.outputName.orEmpty()}"
                DownloadJob.STATUS_CANCELLED -> "Cancelado"
                else -> job.error ?: "Falhou"
            }
            jobProgress.isIndeterminate = job.status == DownloadJob.STATUS_RUNNING && job.progress <= 0
            if (!jobProgress.isIndeterminate) jobProgress.setProgressCompat(job.progress.toInt().coerceIn(0, 100), AppMotion.enabled(root.context))
            jobProgress.visibility = if (job.status == DownloadJob.STATUS_RUNNING) View.VISIBLE else View.GONE
            jobDetails.text = details(job)
            jobDetails.visibility = if (jobDetails.text.isNullOrBlank()) View.GONE else View.VISIBLE
            val active = job.status == DownloadJob.STATUS_RUNNING || job.status == DownloadJob.STATUS_QUEUED
            jobCancelButton.visibility = if (active) View.VISIBLE else View.GONE
            jobCancelButton.setOnClickListener { onCancel(job) }
            jobOpenButton.visibility = if (job.status == DownloadJob.STATUS_COMPLETED) View.VISIBLE else View.GONE
            jobOpenButton.setOnClickListener { onOpen(job) }
            jobShareButton.visibility = jobOpenButton.visibility
            jobShareButton.setOnClickListener { onShare(job) }
            jobRetryButton.visibility = if (job.status in setOf(DownloadJob.STATUS_FAILED, DownloadJob.STATUS_CANCELLED)) View.VISIBLE else View.GONE
            jobRetryButton.setOnClickListener { onRetry(job) }
            jobFallbackButton.visibility = if (
                job.status == DownloadJob.STATUS_FAILED && job.fallbackEligible && job.url != null
            ) View.VISIBLE else View.GONE
            jobFallbackButton.setOnClickListener { onFallback(job) }
            jobErrorButton.visibility = if (
                job.status == DownloadJob.STATUS_FAILED && !job.errorDetail.isNullOrBlank()
            ) View.VISIBLE else View.GONE
            jobErrorButton.setOnClickListener { onShowError(job) }
        }
    }

    private fun details(job: DownloadJob): String {
        val parts = mutableListOf<String>()
        if (job.type == DownloadJob.TYPE_UPSCALE && job.totalBytes > 0) {
            parts += "${job.downloadedBytes} / ${job.totalBytes} frames"
        } else if (job.totalBytes > 0) {
            parts += "${bytes(job.downloadedBytes)} / ${bytes(job.totalBytes)}"
        }
        if (job.speedBytes > 0) parts += "${bytes(job.speedBytes)}/s"
        if (job.etaSeconds >= 0) parts += "restam ${job.etaSeconds}s"
        return parts.joinToString(" · ")
    }

    private fun bytes(value: Long): String {
        val mib = value / 1024.0 / 1024.0
        return if (mib >= 1024) String.format(Locale.US, "%.1f GB", mib / 1024)
        else String.format(Locale.US, "%.1f MB", mib)
    }

    private object Diff : DiffUtil.ItemCallback<DownloadJob>() {
        override fun areItemsTheSame(oldItem: DownloadJob, newItem: DownloadJob) = oldItem.id == newItem.id
        override fun areContentsTheSame(oldItem: DownloadJob, newItem: DownloadJob) = oldItem == newItem
    }
}
