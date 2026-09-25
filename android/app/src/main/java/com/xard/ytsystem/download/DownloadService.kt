package com.xard.ytsystem.download

import android.app.NotificationChannel
import android.annotation.SuppressLint
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.os.StatFs
import android.os.SystemClock
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import android.content.pm.PackageManager
import com.google.gson.Gson
import com.xard.ytsystem.BuildConfig
import com.xard.ytsystem.LocalTools
import com.xard.ytsystem.MainActivity
import com.xard.ytsystem.R
import com.xard.ytsystem.security.SafeDiagnostics
import com.xard.ytsystem.update.RemoteConfigStore
import com.xard.ytsystem.SlucssApplication
import com.xard.ytsystem.api.GifOptions
import com.xard.ytsystem.api.ApiException
import com.xard.ytsystem.data.DownloadJob
import com.xard.ytsystem.upscale.NativeUpscaler
import com.xard.ytsystem.upscale.ThermalCritical
import com.xard.ytsystem.upscale.UpscaleCancelled
import com.xard.ytsystem.upscale.UpscaleOptions
import com.xard.ytsystem.upscale.VideoUpscalePipeline
import com.yausername.youtubedl_android.YoutubeDL
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.coroutines.withContext
import kotlinx.coroutines.NonCancellable
import java.io.File
import java.io.FileOutputStream
import java.util.UUID
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import kotlin.math.roundToInt

class DownloadService : Service() {
    private val serviceScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val dao by lazy { (application as SlucssApplication).database.jobs() }
    private val api by lazy { (application as SlucssApplication).api }
    private val events by lazy { (application as SlucssApplication).downloadEvents }
    private val gson = Gson()
    private var processor: Job? = null
    private val queueSignal = Channel<Unit>(Channel.CONFLATED)
    @Volatile private var activeJobId: String? = null
    @Volatile private var activeProcessId: String? = null
    @Volatile private var activeProcess: Process? = null
    @Volatile private var activeServerTaskId: String? = null
    @Volatile private var activeUpscaler: NativeUpscaler? = null

    // Últimas linhas da ferramenta na tarefa atual: são elas que dizem o motivo
    // real da falha, e agora seguem para a tela em vez de só para o logcat.
    @Volatile private var activeToolOutput: String? = null
    private val cancelled = AtomicBoolean(false)
    private val expired = AtomicBoolean(false)
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        ServiceCompat.startForeground(this, NOTIFICATION_ID,
            notification("Preparando a fila...", 0, false),
            if (Build.VERSION.SDK_INT >= 29) ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC else 0)
        when (intent?.action) {
            ACTION_CANCEL -> cancelJob(intent.getStringExtra(EXTRA_JOB_ID))
            else -> {
                queueSignal.trySend(Unit)
                startProcessor()
            }
        }
        // START_STICKY faria o Android religar este serviço sozinho depois de
        // matá-lo por memória — inclusive muito tempo depois, quando o Doze
        // libera o aparelho por outro motivo qualquer. Sem intent nenhum, ele
        // retomava tarefas antigas da fila sem nenhuma ação da pessoa, parecendo
        // um download começando do nada. A retomada agora só acontece quando o
        // app é reaberto (ver MainActivity.onCreate).
        return START_NOT_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        cancelActiveProcess()
        releaseWakeLock()
        serviceScope.cancel()
        super.onDestroy()
    }

    override fun onTimeout(startId: Int, fgsType: Int) {
        cancelled.set(true)
        cancelActiveProcess()
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf(startId)
    }

    private fun startProcessor() {
        if (processor?.isActive == true) return
        processor = serviceScope.launch {
            dao.recoverInterrupted()
            while (true) {
                val next = dao.nextQueued()
                if (next != null) {
                    executeJob(next)
                    continue
                }
                if (withTimeoutOrNull(1_000) { queueSignal.receive() } == null) break
            }
            activeJobId = null
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
        }
    }

    private suspend fun executeJob(job: DownloadJob) {
        activeJobId = job.id
        var reportingJob = job
        val eventName = events.currentName()
        // Etapa corrente da tarefa: sem ela a falha chega ao log sem dizer se
        // morreu autorizando, preparando as ferramentas, baixando ou salvando.
        var failureStage = "authorizing"
        var keepPartial = job.type == DownloadJob.TYPE_DOWNLOAD
        cancelled.set(false)
        expired.set(false)
        val workDir = File(noBackupFilesDir, "jobs/${job.id}")
        val deadline = serviceScope.launch {
            delay(2 * 60 * 60 * 1000L)
            expired.set(true)
            cancelActiveProcess()
        }
        try {
            if (RemoteConfigStore(this).requiresUpdate) throw ApiException(426, "Atualização obrigatória.")
            acquireWakeLock()
            promoteForeground(job)
            if (job.type != DownloadJob.TYPE_DOWNLOAD) workDir.deleteRecursively()
            if (!workDir.isDirectory && !workDir.mkdirs()) throw java.io.IOException("Falha ao criar diretório da tarefa.")
            update(job, "running", "authorizing", 0f, "Solicitando autorização...")
            val authorizationStarted = SystemClock.elapsedRealtime()
            val authorizedJob = if (job.type == DownloadJob.TYPE_SERVER_DOWNLOAD) {
                job
            } else {
                authorizeAtStart(job)
            }
            Log.i(
                TAG,
                "phase_timing job=${job.id} authorization_ms=" +
                    "${SystemClock.elapsedRealtime() - authorizationStarted}",
            )
            reportingJob = authorizedJob
            events.emit(job, "started", eventName)
            failureStage = "preparing"
            update(authorizedJob, "running", "preparing", 0f, "Preparando a tarefa...")
            ensureFreeSpace(workDir, MIN_FREE_BYTES)
            if (authorizedJob.type in setOf(
                    DownloadJob.TYPE_DOWNLOAD,
                    DownloadJob.TYPE_GIF,
                    DownloadJob.TYPE_COMPATIBILITY,
                )
            ) {
                failureStage = "tools_init"
                val toolsStarted = SystemClock.elapsedRealtime()
                // A análise do link não inicializa o FFmpeg; tarefas locais sim.
                try {
                    LocalTools.ensureInitialized(applicationContext)
                } catch (error: Throwable) {
                    if (error is VirtualMachineError || error is ThreadDeath ||
                        error is LinkageError || error is CancellationException
                    ) throw error
                    throw ToolsUnavailable(errorDetail(error))
                }
                Log.i(
                    TAG,
                    "phase_timing job=${job.id} tools_init_ms=" +
                        "${SystemClock.elapsedRealtime() - toolsStarted}",
                )
            }
            failureStage = "processing"
            checkNotCancelled()
            val result = when (authorizedJob.type) {
                DownloadJob.TYPE_DOWNLOAD -> runYoutubeDl(authorizedJob, workDir)
                DownloadJob.TYPE_GIF -> runGif(authorizedJob, workDir)
                DownloadJob.TYPE_COMPATIBILITY -> runCompatibility(authorizedJob, workDir)
                DownloadJob.TYPE_UPSCALE -> runUpscale(authorizedJob, workDir)
                DownloadJob.TYPE_SERVER_DOWNLOAD -> runServerDownload(authorizedJob, workDir)
                else -> error("Tipo de tarefa desconhecido")
            }
            checkNotCancelled()
            failureStage = "saving"
            update(job, "running", "saving", 99f, "Salvando em Downloads/Slucss...")
            val media = MediaPublisher.publish(this, result, result.name, cancelled::get)
            keepPartial = false
            dao.finish(
                id = job.id,
                status = DownloadJob.STATUS_COMPLETED,
                stage = "completed",
                progress = 100f,
                message = "Concluído.",
                outputUri = media.uri.toString(),
                outputMime = media.mime,
                outputName = media.name,
            )
            events.emit(job, "completed", eventName, fileName = media.name)
            report(authorizedJob, "completed", "completed", 100f, "Operação concluída.")
            showCompletion(job.type, job.title, media.name)
        } catch (_: JobCancelled) {
            keepPartial = false
            dao.finish(
                job.id, DownloadJob.STATUS_CANCELLED, "cancelled", 0f,
                "Cancelado.", error = null,
            )
            report(reportingJob, "cancelled", "cancelled", 0f, "Operação cancelada.")
        } catch (_: UpscaleCancelled) {
            dao.finish(
                job.id, DownloadJob.STATUS_CANCELLED, "cancelled", 0f,
                "Cancelado.", error = null,
            )
            report(reportingJob, "cancelled", "cancelled", 0f, "Operação cancelada.")
        } catch (_: CancellationException) {
            withContext(NonCancellable) {
                dao.finish(job.id, DownloadJob.STATUS_QUEUED, "interrupted", 0f,
                    "Download interrompido. Abra o app para retomar.")
            }
        } catch (error: Throwable) {
            if (error is VirtualMachineError || error is ThreadDeath) throw error
            if (cancelled.get()) {
                keepPartial = false
                dao.finish(job.id, DownloadJob.STATUS_CANCELLED, "cancelled", 0f, "Cancelado.")
                return
            }
            val message = TransferErrors.message(error, failureStage) ?: friendlyError(error)
            val fallback = isFallbackEligible(error)
            val detail = errorDetail(error)
            Log.w(
                TAG,
                "job_failed type=${job.type} stage=$failureStage " +
                    "reason=${error.javaClass.simpleName} detail=$detail",
            )
            dao.finish(
                job.id, DownloadJob.STATUS_FAILED, "failed", 0f,
                message, error = message,
                errorDetail = buildErrorDetail(job, failureStage, error, detail),
                fallbackEligible = fallback,
            )
            report(
                reportingJob, "failed", "failed", 0f,
                "$failureStage/${error.javaClass.simpleName}: $detail".take(REPORT_MESSAGE_LIMIT),
            )
            events.emit(job, "failed", eventName, error = message)
            showFailure(job.title, message)
        } finally {
            deadline.cancel()
            activeProcessId = null
            activeProcess = null
            activeServerTaskId = null
            activeUpscaler = null
            activeToolOutput = null
            if (!keepPartial) workDir.deleteRecursively() else workDir.setLastModified(System.currentTimeMillis())
            releaseWakeLock()
            activeJobId = null
        }
    }

    private suspend fun authorizeAtStart(job: DownloadJob): DownloadJob {
        val permit = when (job.type) {
            DownloadJob.TYPE_DOWNLOAD -> api.authorizeDownload(
                requireNotNull(job.url), job.mode, job.height, job.videoFormat, job.audioFormat,
            )
            DownloadJob.TYPE_GIF -> api.authorizeGif(
                gson.fromJson(job.optionsJson, GifOptions::class.java),
            )
            DownloadJob.TYPE_COMPATIBILITY -> api.authorizeCompatibility()
            DownloadJob.TYPE_UPSCALE -> api.authorizeUpscale(
                gson.fromJson(job.optionsJson, UpscaleOptions::class.java),
            )
            else -> error("Tipo de tarefa desconhecido")
        }
        dao.setOperationId(job.id, permit.operationId)
        return job.copy(operationId = permit.operationId)
    }

    private suspend fun runYoutubeDl(job: DownloadJob, workDir: File): File =
        withContext(Dispatchers.IO) {
            val preparationStarted = SystemClock.elapsedRealtime()
            // Sem cookies o YouTube cobra verificação de robô de parte das redes.
            val cookies = LocalTools.cookiesFile(this@DownloadService, api)
            val url = requireNotNull(job.url)
            val cache = VideoInfoCache.from(this@DownloadService)
            val cachedInfoFile = File(workDir, "analysis.info.json")
            var cachedInfo = cache.copyFreshTo(url, cachedInfoFile)
            if (cachedInfo == null && VideoInfoWarmup.isActive(url)) {
                update(job, "running", "preparing", 0f, "Finalizando a análise do vídeo...")
                val waitStarted = SystemClock.elapsedRealtime()
                val completed = VideoInfoWarmup.await(url, ANALYSIS_WARMUP_WAIT_MS)
                if (completed) cachedInfo = cache.copyFreshTo(url, cachedInfoFile)
                Log.i(
                    TAG,
                    "phase_timing job=${job.id} analysis_warmup_wait_ms=" +
                        "${SystemClock.elapsedRealtime() - waitStarted} hit=${cachedInfo != null}",
                )
            }
            val jsRuntime = LocalTools.jsRuntimeOption(this@DownloadService)
            fun request(info: File?) = YoutubeDlRequestFactory.download(
                job = job,
                workDir = workDir,
                jsRuntime = jsRuntime,
                cookies = cookies,
                cachedInfo = info,
            )

            val processId = "download-${job.id}"
            activeProcessId = processId
            val lastUi = AtomicLong(0)
            val lastReportBucket = AtomicInteger(-1)
            val firstProgressLogged = AtomicBoolean(false)
            // Últimas linhas do yt-dlp: são elas que dizem o motivo real da
            // falha (aviso de runtime, formato ausente, recusa da plataforma).
            val outputTail = ArrayDeque<String>()
            val onProgress: (Float, Long, String) -> Unit = { percent, eta, line ->
                if (firstProgressLogged.compareAndSet(false, true)) {
                    Log.i(
                        TAG,
                        "phase_timing job=${job.id} first_ytdlp_output_ms=" +
                            "${SystemClock.elapsedRealtime() - preparationStarted}",
                    )
                }
                val trimmed = line.trim()
                if (trimmed.isNotEmpty()) {
                    synchronized(outputTail) {
                        outputTail.addLast(trimmed)
                        while (outputTail.size > OUTPUT_TAIL_LINES) outputTail.removeFirst()
                    }
                }
                if (!cancelled.get()) {
                    val parsed = ProgressParser.parse(line, percent, eta)
                    val now = System.currentTimeMillis()
                    if (now - lastUi.get() >= 750 && lastUi.compareAndSet(lastUi.get(), now)) {
                        serviceScope.launch {
                            dao.updateProgress(
                                job.id, DownloadJob.STATUS_RUNNING, parsed.stage,
                                parsed.percent, parsed.downloadedBytes, parsed.totalBytes,
                                parsed.speedBytes, parsed.etaSeconds, stageMessage(parsed.stage),
                            )
                            notifyProgress(stageMessage(parsed.stage), parsed.percent)
                        }
                    }
                    val bucket = parsed.percent.roundToInt().coerceAtLeast(0) / 10
                    if (bucket > lastReportBucket.get() && lastReportBucket.compareAndSet(lastReportBucket.get(), bucket)) {
                        serviceScope.launch {
                            runCatching {
                                report(job, "running", parsed.stage, parsed.percent, stageMessage(parsed.stage))
                            }
                        }
                    }
                }
            }
            var activeRequest = request(cachedInfo)
            Log.i(
                TAG,
                "ytdlp_start job=${job.id} source=" +
                    (if (cachedInfo == null) "url" else "analysis_cache") +
                    " preparation_ms=${SystemClock.elapsedRealtime() - preparationStarted}",
            )
            try {
                LocalTools.withYoutubeDl {
                    YoutubeDL.getInstance().execute(activeRequest, processId, onProgress)
                }
            } catch (firstError: Throwable) {
                if (firstError is VirtualMachineError || firstError is ThreadDeath ||
                    firstError is LinkageError || firstError is CancellationException
                ) throw firstError
                if (!cancelled.get()) {
                    val tail = synchronized(outputTail) { outputTail.joinToString("\n") }
                    activeToolOutput = SafeDiagnostics.redact(tail).ifBlank { null }
                    Log.w(TAG, "ytdlp_output job=${job.id} cached=${cachedInfo != null} tail=${SafeDiagnostics.redact(tail)}")
                }
                checkNotCancelled()
                // O motor nem chegou a executar (aparelho x86 com motor ARM):
                // repetir ou atualizar o yt-dlp falharia igual. Sobe direto e a
                // falha oferece o download pelo servidor.
                if (LocalTools.isLocalEngineUnavailable(errorDetail(firstError))) throw firstError
                if (cachedInfo != null) {
                    // URLs de CDN dentro do info JSON podem expirar antes do TTL.
                    // Nesse caso invalida e faz uma única extração normal.
                    cache.invalidate(url)
                    cachedInfo.delete()
                    synchronized(outputTail) { outputTail.clear() }
                    activeRequest = request(null)
                    Log.i(TAG, "ytdlp_retry_without_cache job=${job.id}")
                    try {
                        LocalTools.withYoutubeDl {
                            YoutubeDL.getInstance().execute(activeRequest, processId, onProgress)
                        }
                    } catch (liveError: Throwable) {
                        if (liveError is VirtualMachineError || liveError is ThreadDeath ||
                            liveError is LinkageError || liveError is CancellationException
                        ) throw liveError
                        checkNotCancelled()
                        if (!isStaleExtractorFailure(liveError)) throw liveError
                        if (!LocalTools.refreshYoutubeDl(applicationContext)) throw liveError
                        checkNotCancelled()
                        Log.i(TAG, "ytdlp_retry_after_update job=${job.id}")
                        LocalTools.withYoutubeDl {
                            YoutubeDL.getInstance().execute(activeRequest, processId, onProgress)
                        }
                    }
                } else {
                    // Sem cache, HTTP 403 com metadados corretos costuma indicar
                    // um extrator defasado. Atualiza e repete uma única vez.
                    if (!isStaleExtractorFailure(firstError)) throw firstError
                    if (!LocalTools.refreshYoutubeDl(applicationContext)) throw firstError
                    checkNotCancelled()
                    Log.i(TAG, "ytdlp_retry_after_update job=${job.id}")
                    LocalTools.withYoutubeDl {
                        YoutubeDL.getInstance().execute(activeRequest, processId, onProgress)
                    }
                }
            }
            checkNotCancelled()
            findResult(workDir)
        }

    private fun isStaleExtractorFailure(error: Throwable): Boolean =
        LocalTools.isStaleExtractorFailure(errorDetail(error))

    /**
     * Texto que a pessoa copia da tela e manda no suporte. Sem ele, a única
     * pista era o logcat — inalcançável para quem só tem o aparelho na mão.
     */
    private fun buildErrorDetail(
        job: DownloadJob,
        stage: String,
        error: Throwable,
        detail: String,
    ): String = buildString {
        appendLine("Slucss ${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE})")
        appendLine("Android ${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT}) ${Build.SUPPORTED_ABIS.firstOrNull()}")
        appendLine("Tarefa: ${job.type} ${job.mode} ${job.height?.let { "${it}p" } ?: "melhor"}")
        job.url?.let { appendLine("Link: $it") }
        appendLine("Etapa: $stage")
        appendLine("Erro: ${error.javaClass.simpleName}: $detail")
        activeToolOutput?.let {
            appendLine()
            appendLine("Saída da ferramenta:")
            append(it)
        }
    }.let(SafeDiagnostics::redact).trim().take(ERROR_DETAIL_SHARE_LIMIT)

    private suspend fun runServerDownload(job: DownloadJob, workDir: File): File {
        val taskId = api.createServerDownload(
            requireNotNull(job.url), job.mode, job.height, job.videoFormat, job.audioFormat,
        )
        activeServerTaskId = taskId
        dao.setOperationId(job.id, taskId)
        var fileUrl: String? = null
        var fileName: String? = null
        val serverStarted = SystemClock.elapsedRealtime()
        while (fileUrl == null) {
            if (SystemClock.elapsedRealtime() - serverStarted > 30 * 60 * 1000L) {
                runCatching { api.cancelServerTask(taskId) }
                throw java.net.SocketTimeoutException("Server processing timed out")
            }
            checkNotCancelled()
            val status = api.serverDownloadStatus(taskId)
            when (status.status) {
                "completed" -> {
                    fileUrl = status.fileUrl ?: error("Link do arquivo pronto ausente")
                    fileName = status.fileName
                }
                "failed" -> error(status.error ?: "O servidor não conseguiu processar este conteúdo.")
                "cancelled" -> throw JobCancelled()
                else -> {
                    dao.updateProgress(
                        job.id, DownloadJob.STATUS_RUNNING, "server_processing",
                        status.progress.coerceIn(0f, 95f), 0, 0, 0, -1,
                        status.message.take(160),
                    )
                    notifyProgress(status.message, status.progress.coerceIn(0f, 95f))
                    delay(1_250)
                }
            }
        }
        val extension = fileName?.substringAfterLast('.', "")?.takeIf { it.length in 2..5 }
            ?: if (job.mode == "a") job.audioFormat else job.videoFormat
        val output = File(workDir, "${safeStem(job.title)}.$extension")
        val transferUi = AtomicLong(0)
        try {
            api.downloadServerFile(
                fileUrl,
                output,
                shouldCancel = cancelled::get,
            ) { downloaded, total ->
                if (downloaded % (16L * 1024 * 1024) < 256 * 1024 &&
                    StatFs(workDir.absolutePath).availableBytes < 64L * 1024 * 1024
                ) throw NoSpace()
                val now = SystemClock.elapsedRealtime()
                val previous = transferUi.get()
                if (downloaded != total && now - previous < 750) return@downloadServerFile
                if (!transferUi.compareAndSet(previous, now)) return@downloadServerFile
                val percent = if (total > 0) 95f + downloaded * 4f / total else 95f
                serviceScope.launch {
                    dao.updateProgress(
                        job.id, DownloadJob.STATUS_RUNNING, "server_downloading",
                        percent.coerceIn(95f, 99f), downloaded, total, 0, -1,
                        "Recebendo arquivo pronto...",
                    )
                    notifyProgress("Recebendo arquivo pronto...", percent)
                }
            }
        } catch (error: Exception) {
            checkNotCancelled()
            throw error
        }
        checkNotCancelled()
        return output
    }

    private suspend fun runGif(job: DownloadJob, workDir: File): File {
        val source = copySource(job, workDir)
        val options = gson.fromJson(job.optionsJson, GifOptions::class.java)
        require(options.end > options.start && options.end - options.start <= 30) {
            "Trecho de GIF inválido"
        }
        val palette = File(workDir, "palette.png")
        val output = File(workDir, safeStem(job.title) + ".gif")
        val duration = options.end - options.start
        val scale = if (options.resolution == "original") "" else {
            val height = options.resolution.removeSuffix("p").toIntOrNull() ?: 480
            ",scale=-2:$height:flags=lanczos"
        }
        runFfmpeg(
            job,
            listOf(
                "-hide_banner", "-loglevel", "error", "-nostdin",
                "-ss", options.start.toString(), "-t", duration.toString(), "-i", source.absolutePath,
                "-map", "0:v:0", "-an", "-vf",
                "fps=${options.fps}$scale,palettegen=max_colors=${options.colors}",
                "-y", palette.absolutePath,
            ),
            0f, 40f, "Criando paleta...",
        )
        runFfmpeg(
            job,
            listOf(
                "-hide_banner", "-loglevel", "error", "-nostdin",
                "-ss", options.start.toString(), "-t", duration.toString(), "-i", source.absolutePath,
                "-i", palette.absolutePath, "-filter_complex",
                "[0:v]fps=${options.fps}$scale[v];[v][1:v]paletteuse=dither=sierra2_4a[out]",
                "-map", "[out]", "-loop", "0", "-progress", "pipe:1", "-nostats",
                "-y", output.absolutePath,
            ),
            40f, 58f, "Criando GIF...",
        )
        if (!output.isFile || output.length() == 0L) error("GIF vazio")
        return output
    }

    private suspend fun runCompatibility(job: DownloadJob, workDir: File): File {
        val source = copySource(job, workDir)
        val output = File(workDir, safeStem(job.title) + "-compatível.mp4")
        runFfmpeg(
            job,
            listOf(
                "-hide_banner", "-loglevel", "error", "-nostdin", "-i", source.absolutePath,
                "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264",
                "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                "-tag:v", "avc1", "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", "-progress", "pipe:1", "-nostats",
                "-y", output.absolutePath,
            ),
            0f, 98f, "Convertendo vídeo...",
        )
        if (!output.isFile || output.length() == 0L) error("Vídeo convertido vazio")
        return output
    }

    private suspend fun runUpscale(job: DownloadJob, workDir: File): File =
        withContext(Dispatchers.IO) {
            val source = android.net.Uri.parse(requireNotNull(job.sourceUri))
            val options = gson.fromJson(job.optionsJson, UpscaleOptions::class.java)
            val output = File(
                workDir,
                "${safeStem(job.title)} - IA ${options.scale}x.mp4",
            )
            var lastBackendBucket = -1
            val progressThrottle = ProgressThrottle()
            val result = VideoUpscalePipeline(
                context = this@DownloadService,
                source = source,
                output = output,
                options = options,
                shouldCancel = cancelled::get,
                onProgress = { progress ->
                    if (progressThrottle.shouldPublish(SystemClock.elapsedRealtime(), progress.stage)) {
                        serviceScope.launch {
                            dao.updateProgress(
                                job.id,
                                DownloadJob.STATUS_RUNNING,
                                progress.stage,
                                progress.percent,
                                progress.processedFrames.toLong(),
                                progress.totalFrames.toLong(),
                                0,
                                progress.etaSeconds,
                                progress.message.take(160),
                            )
                            notifyProgress(progress.message, progress.percent)
                        }
                    }
                    val bucket = progress.percent.toInt() / 10
                    if (bucket > lastBackendBucket) {
                        lastBackendBucket = bucket
                        serviceScope.launch {
                            report(
                                job,
                                "running",
                                progress.stage,
                                progress.percent,
                                progress.message,
                            )
                        }
                    }
                },
                onEngineChanged = { activeUpscaler = it },
            ).run()
            Log.i(
                TAG,
                "upscale_completed job=${job.id} model=${options.model} scale=${options.scale} " +
                    "vulkan=${result.usedVulkan} gpu=${result.gpuName.take(80)} " +
                    "encoder=${result.encoderName} frames=${result.processedFrames} " +
                    "source_fps=${result.sourceFrameRate} output_fps=${result.outputFrameRate} " +
                    "elapsed_ms=${result.elapsedMillis} thermal_max=${result.maximumThermalStatus} " +
                    "battery_used=${result.batteryUsedPercent ?: -1}",
            )
            if (!output.isFile || output.length() == 0L) error("O vídeo ampliado ficou vazio.")
            output
        }

    private suspend fun runFfmpeg(
        job: DownloadJob,
        arguments: List<String>,
        base: Float,
        span: Float,
        message: String,
    ) = withContext(Dispatchers.IO) {
        val command = listOf(LocalTools.ffmpegExecutable(this@DownloadService)) + arguments
        val builder = ProcessBuilder(command).redirectErrorStream(true)
        builder.environment()["LD_LIBRARY_PATH"] =
            LocalTools.ffmpegLibraryPath(this@DownloadService)
        val process = builder.start()
        activeProcess = process
        update(job, "running", "processing", base, message)
        var lastNotification = SystemClock.elapsedRealtime()
        val tail = StringBuilder()
        try {
            process.inputStream.bufferedReader(Charsets.UTF_8).useLines { lines ->
                lines.forEach { line ->
                    checkNotCancelled()
                    if (tail.length + line.length > ERROR_TAIL_LIMIT) {
                        tail.delete(0, minOf(tail.length, line.length + 256))
                    }
                    tail.append(line.take(512)).append('\n')
                    val now = SystemClock.elapsedRealtime()
                    if (now - lastNotification >= 750) {
                        lastNotification = now
                        // Sem duração confiável, a barra avança até o limite
                        // da etapa sem inventar um percentual baseado em bytes.
                        notifyProgress(message, base)
                    }
                }
            }
        } catch (error: Exception) {
            checkNotCancelled()
            throw error
        }
        val exit = process.waitFor()
        activeProcess = null
        checkNotCancelled()
        if (exit != 0) throw ToolFailure(tail.takeLast(ERROR_TAIL_LIMIT).toString())
        update(job, "running", "processing", base + span, message)
    }

    private suspend fun copySource(job: DownloadJob, workDir: File): File =
        withContext(Dispatchers.IO) {
            val uri = android.net.Uri.parse(requireNotNull(job.sourceUri))
            val source = File(workDir, "source.bin")
            val declaredSize = contentResolver.query(
                uri, arrayOf(android.provider.OpenableColumns.SIZE), null, null, null,
            )?.use { cursor ->
                if (cursor.moveToFirst() && !cursor.isNull(0)) cursor.getLong(0) else 0L
            } ?: 0L
            if (declaredSize > 0 &&
                StatFs(workDir.absolutePath).availableBytes < declaredSize + 128L * 1024 * 1024
            ) throw NoSpace()
            contentResolver.openInputStream(uri)?.use { input ->
                FileOutputStream(source).use { output ->
                    val buffer = ByteArray(256 * 1024)
                    var copied = 0L
                    while (true) {
                        checkNotCancelled()
                        val read = input.read(buffer)
                        if (read < 0) break
                        output.write(buffer, 0, read)
                        copied += read
                        if (copied % (16L * 1024 * 1024) < buffer.size &&
                            StatFs(workDir.absolutePath).availableBytes < 64L * 1024 * 1024
                        ) throw NoSpace()
                    }
                }
            } ?: error("Arquivo de origem indisponível")
            if (source.length() == 0L) error("Arquivo de origem vazio")
            source
        }

    private fun findResult(workDir: File): File = workDir.listFiles()
        ?.filter { file ->
            file.isFile && file.length() > 0 &&
                !file.name.endsWith(".part") && !file.name.endsWith(".ytdl") &&
                !file.name.endsWith(".json")
        }
        ?.maxByOrNull(File::length)
        ?: error("Nenhum arquivo final foi criado")

    private suspend fun update(
        job: DownloadJob,
        status: String,
        stage: String,
        progress: Float,
        message: String,
    ) {
        dao.updateProgress(job.id, status, stage, progress, 0, 0, 0, -1, message)
        notifyProgress(message, progress)
    }

    private fun report(
        job: DownloadJob,
        status: String,
        stage: String,
        progress: Float,
        message: String,
    ) {
        if (job.operationId.isBlank()) return
        (application as SlucssApplication).reportInBackground {
            api.reportLocalProgress(job.operationId, status, stage, progress, message)
        }
    }

    private fun cancelJob(requestedId: String?) {
        if (requestedId == null || requestedId == activeJobId) {
            cancelled.set(true)
            cancelActiveProcess()
            return
        }
        serviceScope.launch {
            dao.get(requestedId)?.takeIf { it.status == DownloadJob.STATUS_QUEUED }?.let {
                dao.finish(
                    it.id, DownloadJob.STATUS_CANCELLED, "cancelled", 0f, "Cancelado.",
                )
                report(it, "cancelled", "cancelled", 0f, "Operação cancelada.")
            }
            startProcessor()
        }
    }

    private fun cancelActiveProcess() {
        activeServerTaskId?.let { taskId ->
            serviceScope.launch { runCatching { api.cancelServerTask(taskId) } }
        }
        activeProcessId?.let(LocalProcessKiller::destroyYoutubeDl)
        activeUpscaler?.cancel()
        activeProcess?.let { process ->
            runCatching {
                process.destroy()
                if (!process.waitFor(2, java.util.concurrent.TimeUnit.SECONDS)) {
                    process.destroyForcibly()
                    process.waitFor(2, java.util.concurrent.TimeUnit.SECONDS)
                }
            }
        }
    }

    private fun checkNotCancelled() {
        if (expired.get()) throw java.net.SocketTimeoutException("Operation timed out")
        if (cancelled.get()) throw JobCancelled()
    }

    private fun ensureFreeSpace(directory: File, required: Long) {
        if (StatFs(directory.absolutePath).availableBytes < required) {
            throw NoSpace()
        }
    }

    private fun acquireWakeLock() {
        releaseWakeLock()
        wakeLock = (getSystemService(POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "$packageName:download")
            .apply { acquire(6 * 60 * 60 * 1000L) }
    }

    private fun promoteForeground(job: DownloadJob) {
        val type = if (job.type == DownloadJob.TYPE_UPSCALE && Build.VERSION.SDK_INT >= 35) {
            ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROCESSING
        } else if (Build.VERSION.SDK_INT >= 29) {
            ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
        } else 0
        ServiceCompat.startForeground(
            this,
            NOTIFICATION_ID,
            notification("Preparando a tarefa...", 0, true),
            type,
        )
    }

    private fun releaseWakeLock() {
        wakeLock?.takeIf { it.isHeld }?.release()
        wakeLock = null
    }

    @SuppressLint("MissingPermission")
    private fun notifyProgress(message: String, progress: Float) {
        if (!notificationsAllowed()) return
        NotificationManagerCompat.from(this).notify(
            NOTIFICATION_ID,
            notification(message, progress.roundToInt(), true),
        )
    }

    private fun notification(message: String, progress: Int, cancellable: Boolean) =
        NotificationCompat.Builder(this, CHANNEL_DOWNLOADS)
            .setSmallIcon(R.drawable.ic_slucss)
            .setContentTitle("Slucss System")
            .setContentText(message)
            .setOnlyAlertOnce(true)
            .setOngoing(true)
            .setProgress(100, progress.coerceIn(0, 100), progress <= 0)
            .setContentIntent(
                PendingIntent.getActivity(
                    this, 1, Intent(this, MainActivity::class.java),
                    PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
                ),
            )
            .apply {
                if (cancellable && activeJobId != null) {
                    addAction(0, "Cancelar", cancelIntent(activeJobId!!))
                }
            }
            .build()

    private fun cancelIntent(jobId: String): PendingIntent = PendingIntent.getService(
        this,
        jobId.hashCode(),
        Intent(this, DownloadService::class.java).apply {
            action = ACTION_CANCEL
            putExtra(EXTRA_JOB_ID, jobId)
        },
        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
    )

    @SuppressLint("MissingPermission")
    private fun showCompletion(type: String, title: String, name: String) {
        if (!notificationsAllowed()) return
        NotificationManagerCompat.from(this).notify(
            title.hashCode(),
            NotificationCompat.Builder(this, CHANNEL_DOWNLOADS)
                .setSmallIcon(R.drawable.ic_slucss)
                .setContentTitle(
                    if (type == DownloadJob.TYPE_UPSCALE) "Upscale concluído"
                    else "Download concluído",
                )
                .setContentText(name)
                .setAutoCancel(true)
                .setContentIntent(
                    PendingIntent.getActivity(
                        this, 2, Intent(this, MainActivity::class.java),
                        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
                    ),
                ).build(),
        )
    }

    @SuppressLint("MissingPermission")
    private fun showFailure(title: String, message: String) {
        if (!notificationsAllowed()) return
        NotificationManagerCompat.from(this).notify(
            title.hashCode(),
            NotificationCompat.Builder(this, CHANNEL_DOWNLOADS)
                .setSmallIcon(R.drawable.ic_slucss)
                .setContentTitle("Não foi possível concluir")
                .setContentText(message)
                .setAutoCancel(true)
                .build(),
        )
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_DOWNLOADS,
                getString(R.string.notification_channel_downloads),
                NotificationManager.IMPORTANCE_LOW,
            )
            getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
        }
    }

    private fun notificationsAllowed(): Boolean =
        Build.VERSION.SDK_INT < 33 || ContextCompat.checkSelfPermission(
            this, android.Manifest.permission.POST_NOTIFICATIONS,
        ) == PackageManager.PERMISSION_GRANTED

    /**
     * Motivo cru da falha, em uma linha e limitado. Sem ele o log guarda apenas
     * o nome da classe e a falha fica indepurável no aparelho do usuário.
     */
    private fun errorDetail(error: Throwable): String {
        val lines = (error.message ?: error.cause?.message).orEmpty()
            .lines().map(String::trim).filter(String::isNotEmpty)
        // O yt-dlp mistura WARNING e ERROR na mesma exceção. Sem escolher a
        // linha de ERROR, um aviso longo consumia sozinho o limite da mensagem
        // e o motivo verdadeiro nunca chegava ao log.
        val chosen = lines.firstOrNull { it.startsWith("ERROR", ignoreCase = true) }
            ?: lines.firstOrNull { !it.startsWith("WARNING", ignoreCase = true) }
            ?: lines.firstOrNull()
        return SafeDiagnostics.redact(chosen.orEmpty())
            .replace(Regex("\\s+"), " ")
            .take(ERROR_DETAIL_LIMIT)
            .ifBlank { error.javaClass.simpleName }
    }

    private fun friendlyError(error: Throwable): String = when (error) {
        is NoSpace -> "Não há espaço livre suficiente no aparelho."
        is ThermalCritical ->
            "O Android informou temperatura crítica. O Upscale foi interrompido com segurança."
        is ToolFailure -> "O FFmpeg não conseguiu processar este arquivo."
        is ToolsUnavailable ->
            "Não foi possível preparar as ferramentas de mídia. Libere espaço no aparelho e tente novamente."
        is LinkageError -> "Este aparelho não conseguiu iniciar as ferramentas de mídia. Atualize o aplicativo."
        else -> {
            val raw = error.message.orEmpty()
            when {
                error is UnsatisfiedLinkError ->
                    "O engine nativo de Upscale não é compatível com a arquitetura deste aparelho."
                error is ApiException && error.statusCode == 426 ->
                    "Atualize o Slucss System para continuar."
                LocalTools.isLocalEngineUnavailable(raw) ->
                    "Este aparelho (processador x86, comum em emuladores) não executa o " +
                        "motor de download do aplicativo. Toque em \"Tentar servidor\" " +
                        "para baixar pelo servidor."
                raw.contains("Sem conexão", true) ->
                    "Sem conexão com a API. Verifique sua internet."
                raw.contains("no space", true) || raw.contains("ENOSPC", true) ->
                    "Não há espaço livre suficiente no aparelho."
                raw.contains("Unsupported URL", true) -> "Este link ainda não é suportado."
                raw.contains("not a bot", true) || raw.contains("Sign in to confirm", true) ->
                    "A plataforma pediu verificação para este download. Tente novamente em alguns minutos."
                // Trechos completos de propósito: "age" sozinho casa dentro de
                // "webpage" e classificaria erros comuns como restrição de idade.
                raw.contains("age-restricted", true) || raw.contains("confirm your age", true) ->
                    "Este vídeo tem restrição de idade e não pode ser baixado."
                raw.contains("not available in your country", true) ||
                    raw.contains("geo-restricted", true) || raw.contains("geo restriction", true) ->
                    "Este vídeo não está disponível na sua região."
                raw.contains("video unavailable", true) || raw.contains("has been removed", true) ->
                    "Este vídeo não está mais disponível na plataforma."
                raw.contains("HTTP Error 403", true) ->
                    "A plataforma recusou o download deste arquivo. Tente novamente."
                raw.contains("private", true) || raw.contains("login", true) ->
                    "Este conteúdo exige acesso ou login na plataforma de origem."
                raw.contains("format", true) -> "A qualidade escolhida não está disponível."
                else -> "Não foi possível processar este conteúdo. Verifique o link e tente novamente."
            }
        }
    }

    private fun isFallbackEligible(error: Throwable): Boolean {
        if (error is NoSpace || error is ApiException || cancelled.get()) return false
        val raw = error.message.orEmpty()
        if (raw.contains("no space", true) || raw.contains("ENOSPC", true)) return false
        // A verificação de robô e o 403 na mídia atingem só a extração local,
        // que não tem cookies. O servidor tem, então vale oferecer o reenvio.
        // Aparelho x86 sem motor ARM executável: o servidor é o ÚNICO caminho.
        return error is ToolFailure || raw.contains("Unsupported URL", true) ||
            raw.contains("format", true) || raw.contains("not a bot", true) ||
            raw.contains("Sign in to confirm", true) || raw.contains("HTTP Error 403", true) ||
            LocalTools.isLocalEngineUnavailable(raw)
    }

    private fun stageMessage(stage: String) = when (stage) {
        "merging" -> "Juntando vídeo e áudio..."
        "processing" -> "Processando..."
        else -> "Baixando..."
    }

    private fun safeStem(value: String) = value
        .replace(Regex("[\\/:*?\"<>|\\u0000-\\u001f]"), "_")
        .trim().take(120).ifBlank { "Slucss" }

    private class JobCancelled : Exception()
    private class NoSpace : Exception()
    private class ToolFailure(message: String) : Exception(message)
    private class ToolsUnavailable(message: String) : Exception(message)

    companion object {
        private const val TAG = "SlucssDownload"
        private const val CHANNEL_DOWNLOADS = "slucss_downloads"
        private const val NOTIFICATION_ID = 4101
        private const val ACTION_PROCESS = "com.xard.ytsystem.PROCESS_QUEUE"
        private const val ACTION_CANCEL = "com.xard.ytsystem.CANCEL_JOB"
        private const val EXTRA_JOB_ID = "job_id"
        private const val MIN_FREE_BYTES = 256L * 1024 * 1024
        private const val ANALYSIS_WARMUP_WAIT_MS = 55_000L
        private const val ERROR_TAIL_LIMIT = 8 * 1024
        private const val ERROR_DETAIL_LIMIT = 300
        // Cabe numa mensagem de suporte sem estourar a linha do banco.
        private const val ERROR_DETAIL_SHARE_LIMIT = 4000
        private const val OUTPUT_TAIL_LINES = 12
        // Teto do campo "message" aceito por /local/{id}/progress na API.
        private const val REPORT_MESSAGE_LIMIT = 160

        fun start(context: Context) {
            ContextCompat.startForegroundService(
                context,
                Intent(context, DownloadService::class.java).setAction(ACTION_PROCESS),
            )
        }

        fun cancel(context: Context, jobId: String) {
            ContextCompat.startForegroundService(
                context,
                Intent(context, DownloadService::class.java).apply {
                    action = ACTION_CANCEL
                    putExtra(EXTRA_JOB_ID, jobId)
                },
            )
        }
    }
}
