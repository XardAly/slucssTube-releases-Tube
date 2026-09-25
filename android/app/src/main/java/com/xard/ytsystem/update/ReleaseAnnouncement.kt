package com.xard.ytsystem.update

import android.content.Context
import androidx.appcompat.app.AppCompatActivity
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.xard.ytsystem.BuildConfig
import com.xard.ytsystem.api.AndroidManifest
import com.xard.ytsystem.databinding.DialogReleaseAnnouncementBinding

/** Bundled artwork also works without an extra image request. */
object ReleaseAnnouncement {
    private fun content(activity: AppCompatActivity, version: String, message: String) =
        DialogReleaseAnnouncementBinding.inflate(activity.layoutInflater).apply {
            releaseVersion.text = "Slucss System $version"
            releaseMessage.text = message
        }.root

    fun showUpdate(activity: AppCompatActivity, manifest: AndroidManifest, required: Boolean,
                   onDownload: () -> Unit) = MaterialAlertDialogBuilder(activity)
        .setTitle(if (required) "Atualização necessária" else "Nova atualização disponível")
        .setView(content(activity, manifest.latestVersion,
            manifest.changelog.joinToString("\n") { "• $it" }.ifBlank { "Melhorias e correções." }))
        .setCancelable(!required)
        .setNegativeButton(if (required) null else "Depois", null)
        .setPositiveButton("Baixar") { _, _ -> onDownload() }
        .show()

    /** Call only after a successful, verified server manifest for this build. */
    fun showOnlineIfNeeded(activity: AppCompatActivity): Boolean {
        val prefs = activity.getSharedPreferences("release_announcement", Context.MODE_PRIVATE)
        if (prefs.getInt("version_seen", 0) >= BuildConfig.VERSION_CODE) return false
        MaterialAlertDialogBuilder(activity)
            .setTitle("Aplicativo online")
            .setView(content(activity, BuildConfig.VERSION_NAME,
                "Conexão com o servidor confirmada. As melhorias desta versão já estão disponíveis no aplicativo."))
            .setPositiveButton("Continuar", null)
            .show()
        prefs.edit().putInt("version_seen", BuildConfig.VERSION_CODE).apply()
        return true
    }
}
