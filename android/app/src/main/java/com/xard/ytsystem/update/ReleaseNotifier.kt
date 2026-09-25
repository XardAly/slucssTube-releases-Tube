package com.xard.ytsystem.update

import android.Manifest
import android.annotation.SuppressLint
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.job.JobInfo
import android.app.job.JobParameters
import android.app.job.JobScheduler
import android.app.job.JobService
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import com.xard.ytsystem.BuildConfig
import com.xard.ytsystem.MainActivity
import com.xard.ytsystem.R
import com.xard.ytsystem.SlucssApplication
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.coroutines.CancellationException
import com.xard.ytsystem.api.AndroidManifest

/** Shared, persisted notification policy for foreground and scheduled checks. */
object ReleaseNotifier {
    @SuppressLint("MissingPermission")
    @Synchronized
    fun notify(context: Context, manifest: AndroidManifest) {
        if (!podeNotificar(context)) return
        val versao = manifest.latestVersion
        val versionCode = manifest.latestVersionCode
        val release = ReleasePolicy.key(manifest)
        val preferencias = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        // Sem esta trava o usuário receberia o mesmo aviso a cada ciclo até
        // decidir atualizar.
        if (!ReleasePolicy.shouldNotify(BuildConfig.VERSION_CODE, versionCode, release,
                preferencias.getString("release_notified", "").orEmpty(), preferencias.getInt(KEY_AVISADO, 0))) return

        val gerenciador = context.getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            gerenciador.createNotificationChannel(
                NotificationChannel(
                    CHANNEL_UPDATES,
                    context.getString(R.string.notification_channel_updates),
                    NotificationManager.IMPORTANCE_DEFAULT,
                ),
            )
        }
        if (!NotificationManagerCompat.from(context).areNotificationsEnabled() ||
            gerenciador.getNotificationChannel(CHANNEL_UPDATES)?.importance == NotificationManager.IMPORTANCE_NONE) return
        NotificationManagerCompat.from(context).notify(
            NOTIFICATION_ID,
            NotificationCompat.Builder(context, CHANNEL_UPDATES)
                .setSmallIcon(R.drawable.ic_slucss)
                .setContentTitle("Nova atualização disponível")
                .setContentText("Versão $versao disponível. Atualize para receber melhorias e correções.")
                .setStyle(NotificationCompat.BigTextStyle().bigText("Uma nova versão do aplicativo está disponível. Atualize para receber as melhorias e correções."))
                .setAutoCancel(true)
                .setContentIntent(
                    PendingIntent.getActivity(
                        context,
                        3,
                        Intent(context, MainActivity::class.java)
                            .setAction(UpdateCheckJob.ACTION_UPDATE)
                            .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP),
                        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
                    ),
                ).build(),
        )
        preferencias.edit().putInt(KEY_AVISADO, versionCode).putString("release_notified", release).commit()
    }

    private fun podeNotificar(context: Context): Boolean =
        Build.VERSION.SDK_INT < 33 || ContextCompat.checkSelfPermission(
            context, Manifest.permission.POST_NOTIFICATIONS,
        ) == PackageManager.PERMISSION_GRANTED

    private const val CHANNEL_UPDATES = "slucss_updates"
    private const val NOTIFICATION_ID = 4103
    private const val PREFS = "update_check"
    private const val KEY_AVISADO = "versao_avisada"
}
