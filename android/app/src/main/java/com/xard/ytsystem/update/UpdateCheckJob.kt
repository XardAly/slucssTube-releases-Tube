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

/**
 * Verifica em segundo plano se saiu versão nova e avisa por notificação.
 *
 * Antes disso o usuário só descobria a atualização ao abrir o aplicativo, porque
 * a checagem acontecia apenas na MainActivity.
 */
class UpdateCheckJob : JobService() {
    private var trabalho: Job? = null

    override fun onStartJob(params: JobParameters?): Boolean {
        trabalho = CoroutineScope(Dispatchers.IO).launch {
            var repetir = false
            try {
                val api = (application as SlucssApplication).api
                val manifest = api.androidManifest()
                RemoteConfigStore(this@UpdateCheckJob).update(manifest)
                if (manifest.latestVersionCode > BuildConfig.VERSION_CODE) {
                    ReleaseNotifier.notify(this@UpdateCheckJob, manifest)
                }
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (error: Exception) {
                // Sem rede ou API fora: tentar de novo é melhor do que perder o
                // ciclo inteiro, mas a falha nunca pode derrubar o aplicativo.
                Log.w(TAG, "update_check_failed reason=${error.javaClass.simpleName}")
                repetir = true
            } finally {
                if (kotlin.coroutines.coroutineContext[Job]?.isActive == true) jobFinished(params, repetir)
            }
        }
        return true
    }

    override fun onStopJob(params: JobParameters?): Boolean {
        trabalho?.cancel()
        return true
    }

    companion object {
        private const val TAG = "SlucssUpdateCheck"
        private const val JOB_ID = 4102
        private const val CHANNEL_UPDATES = "slucss_updates"
        private const val NOTIFICATION_ID = 4103
        private const val PREFS = "update_check"
        private const val KEY_AVISADO = "versao_avisada"
        const val ACTION_UPDATE = "com.xard.ytsystem.OPEN_UPDATE"
        private val INTERVALO_MS = 15L * 60 * 1000

        /**
         * Agenda a verificação periódica. Reagendar a cada abertura reiniciaria
         * o ciclo e o aviso nunca chegaria em quem abre o aplicativo com
         * frequência — por isso respeita um agendamento já existente.
         */
        fun schedule(context: Context) {
            val scheduler = context.getSystemService(JobScheduler::class.java) ?: return
            if (scheduler.getPendingJob(JOB_ID)?.intervalMillis == INTERVALO_MS) return
            scheduler.schedule(
                JobInfo.Builder(JOB_ID, ComponentName(context, UpdateCheckJob::class.java))
                    .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                    .setPeriodic(INTERVALO_MS)
                    // Sem isto o Android descarta o job a cada reinício do
                    // aparelho, e ele só volta quando a pessoa abre o app de
                    // novo — quem reinicia o celular e demora a reabrir nunca
                    // recebe o aviso mesmo com uma versão nova publicada há dias.
                    .setPersisted(true)
                    .build(),
            )
        }
    }
}
