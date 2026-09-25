package com.xard.ytsystem

import com.xard.ytsystem.identity.LocalNameStore
import com.xard.ytsystem.ui.AppMotion
import com.xard.ytsystem.security.SafeDiagnostics
import com.xard.ytsystem.download.TransferErrors
import com.xard.ytsystem.update.ReleasePolicy
import com.xard.ytsystem.update.ReleaseAnnouncement
import com.xard.ytsystem.update.UpdateCheckJob
import kotlinx.coroutines.Job
import androidx.recyclerview.widget.SimpleItemAnimator
import android.Manifest
import android.app.DownloadManager
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.BitmapFactory
import android.graphics.Typeface
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import android.provider.OpenableColumns
import android.util.Log
import android.view.inputmethod.EditorInfo
import android.view.View
import android.widget.ArrayAdapter
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.updateLayoutParams
import androidx.core.view.updatePadding
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.repeatOnLifecycle
import androidx.core.widget.doAfterTextChanged
import androidx.recyclerview.widget.LinearLayoutManager
import com.fasterxml.jackson.databind.ObjectMapper
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.gson.Gson
import com.xard.ytsystem.api.AndroidManifest
import com.xard.ytsystem.api.GifOptions
import com.xard.ytsystem.api.RemoteVideoInfo
import com.xard.ytsystem.data.DownloadJob
import com.xard.ytsystem.databinding.ActivityMainBinding
import com.xard.ytsystem.download.DownloadService
import com.xard.ytsystem.download.LocalProcessKiller
import com.xard.ytsystem.download.VideoInfoCache
import com.xard.ytsystem.download.VideoInfoWarmup
import com.xard.ytsystem.ui.JobAdapter
import com.xard.ytsystem.ui.QueueFilter
import com.xard.ytsystem.update.ApkUpdater
import com.xard.ytsystem.update.RemoteConfigStore
import com.xard.ytsystem.upscale.DeviceCapabilities
import com.xard.ytsystem.upscale.UpscaleModel
import com.xard.ytsystem.upscale.UpscaleOptions
import com.xard.ytsystem.upscale.UpscaleVersions
import com.xard.ytsystem.upscale.VideoUpscalePipeline
import com.yausername.youtubedl_android.YoutubeDL
import com.yausername.youtubedl_android.YoutubeDLRequest
import com.yausername.youtubedl_android.mapper.VideoInfo
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.async
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.runInterruptible
import kotlinx.coroutines.selects.select
import kotlinx.coroutines.supervisorScope
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.withContext
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.util.UUID

class MainActivity : AppCompatActivity() {
    private lateinit var binding: ActivityMainBinding
    private val app by lazy { application as SlucssApplication }
    private val dao by lazy { app.database.jobs() }
    private val remoteConfig by lazy { RemoteConfigStore(this) }
    private val updater by lazy { ApkUpdater(this) }
    private val videoInfoMapper = ObjectMapper()
    private val upscaleModels = listOf(
        UpscaleModel.ANIME_VIDEO_V3,
        UpscaleModel.REAL_CUGAN,
    )
    private var currentSection = -1
    private var analysisRunning = false
    private var analysisTask: Job? = null
    private var thumbnailJob: Job? = null
    private var thumbnailGeneration = 0
    private var enqueuePending = false
    private var analyzedUrl: String? = null
    private var analyzedTitle: String = "Vídeo"
    private var qualityHeights: List<Int?> = listOf(null)
    private var gifSource: Uri? = null
    private var gifSourceName: String = "Vídeo"
    private var compatibilitySource: Uri? = null
    private var compatibilitySourceName: String = "Vídeo"
    private var upscaleSource: Uri? = null
    private var upscaleSourceName: String = "Vídeo"
    private var pendingUpdateApk: File? = null
    private var pendingUpdateVersionCode: Int? = null
    private var pendingUpdateRequired = false
    private var waitingForUpdatePermission = false
    private var queueFilter = QueueFilter.ALL
    private var latestJobs: List<DownloadJob> = emptyList()
    private lateinit var jobsAdapter: JobAdapter
    private lateinit var recentAdapter: JobAdapter

    private sealed interface AnalysisResult {
        data class LocalSuccess(val info: VideoInfo) : AnalysisResult
        data class RemoteSuccess(val info: RemoteVideoInfo) : AnalysisResult
        data class LocalFailure(val error: Throwable) : AnalysisResult
        data class RemoteFailure(val error: Throwable) : AnalysisResult
    }

    private val pickGif = registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) {
            retainReadPermission(uri)
            gifSource = uri
            gifSourceName = displayName(uri) ?: "Vídeo"
            binding.gifSourceText.text = gifSourceName
        }
    }
    private val pickCompatibility = registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) {
            retainReadPermission(uri)
            compatibilitySource = uri
            compatibilitySourceName = displayName(uri) ?: "Vídeo"
            binding.compatibilitySourceText.text = compatibilitySourceName
        }
    }
    private val pickUpscale = registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) {
            retainReadPermission(uri)
            upscaleSource = uri
            upscaleSourceName = displayName(uri) ?: "Vídeo"
            binding.upscaleSourceText.text = upscaleSourceName
            inspectUpscaleHardware()
        }
    }
    private val requestNotification = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { }
    private val requestLegacyStorage = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted ->
        if (!granted) toast("A permissão é necessária para salvar no Android 8/9.")
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (LocalNameStore(this).name.isBlank()) {
            startActivity(Intent(this, WelcomeActivity::class.java))
            finish()
            return
        }
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        setupSystemBars()
        setupTabs()
        setupSpinners()
        setupActions()
        setupQueue()
        if (savedInstanceState != null) {
            gifSource = savedInstanceState.getString("gifSource")?.let(Uri::parse)
            gifSourceName = savedInstanceState.getString("gifSourceName") ?: "Vídeo"
            compatibilitySource = savedInstanceState.getString("compatibilitySource")?.let(Uri::parse)
            compatibilitySourceName = savedInstanceState.getString("compatibilitySourceName") ?: "Vídeo"
            upscaleSource = savedInstanceState.getString("upscaleSource")?.let(Uri::parse)
            upscaleSourceName = savedInstanceState.getString("upscaleSourceName") ?: "Vídeo"
            gifSource?.let { binding.gifSourceText.text = gifSourceName }
            compatibilitySource?.let { binding.compatibilitySourceText.text = compatibilitySourceName }
            upscaleSource?.let { binding.upscaleSourceText.text = upscaleSourceName }
            binding.bottomNavigation.selectedItemId = savedInstanceState.getInt("selectedTab", R.id.navigationDownload)
            binding.queueFilters.check(savedInstanceState.getInt("queueFilter", R.id.filterAll))
        }
        requestRuntimePermissions()
        lifecycleScope.launch { checkForUpdates(showWhenCurrent = false) }
        // Retoma tarefas que ficaram pendentes de uma sessão anterior (ex.: o
        // serviço foi morto por memória no meio de um download). Isso fica
        // atrelado à reabertura do app de propósito: o serviço não se religa
        // mais sozinho em segundo plano (ver DownloadService.onStartCommand).
        lifecycleScope.launch {
            if (withContext(Dispatchers.IO) { dao.countActive() } > 0) {
                DownloadService.start(this@MainActivity)
            }
        }
    }

    override fun onResume() {
        super.onResume()
        if (!::binding.isInitialized) return
        if (!waitingForUpdatePermission || pendingUpdateApk == null) return
        waitingForUpdatePermission = false
        if (updater.needsUnknownSourcesPermission()) {
            toast("A permissão não foi concedida. O APK verificado continua pronto.")
            showUpdateReadyDialog(pendingUpdateRequired)
        } else {
            openPendingUpdate()
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        if (::binding.isInitialized && intent.action == UpdateCheckJob.ACTION_UPDATE) {
            lifecycleScope.launch { checkForUpdates(showWhenCurrent = true) }
        }
    }

    private fun setupTabs() {
        binding.bottomNavigation.isItemActiveIndicatorEnabled = true
        binding.bottomNavigation.setOnItemSelectedListener { item ->
            showSection(
                when (item.itemId) {
                    R.id.navigationGif -> 1
                    R.id.navigationCompatibility -> 2
                    R.id.navigationUpscale -> 3
                    R.id.navigationQueue -> 4
                    else -> 0
                },
            )
            true
        }
        binding.bottomNavigation.selectedItemId = R.id.navigationDownload
        showSection(0)
    }

    private fun setupSystemBars() {
        WindowCompat.setDecorFitsSystemWindows(window, false)
        ViewCompat.setOnApplyWindowInsetsListener(binding.root) { _, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            val keyboard = insets.getInsets(WindowInsetsCompat.Type.ime())
            val typing = insets.isVisible(WindowInsetsCompat.Type.ime())
            binding.root.updatePadding(bottom = if (typing) keyboard.bottom else 0)
            binding.bottomNavigation.visibility = if (typing) View.GONE else View.VISIBLE
            binding.topBar.updatePadding(top = bars.top)
            binding.topBar.updateLayoutParams { height = dp(64) + bars.top }
            binding.bottomNavigation.updatePadding(bottom = bars.bottom)
            binding.bottomNavigation.updateLayoutParams { height = dp(80) + bars.bottom }
            insets
        }
        ViewCompat.requestApplyInsets(binding.root)
    }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    private fun showSection(index: Int) {
        // Keep Android from auto-scrolling a newly shown form to its first editable field.
        binding.root.requestFocus()
        val sections = listOf(binding.downloadSection, binding.gifSection, binding.compatibilitySection,
            binding.upscaleSection, binding.queueSection)
        sections.forEachIndexed { position, view ->
            if (position != index) AppMotion.reset(view)
            view.visibility = if (position == index) View.VISIBLE else View.GONE
        }
        if (index != currentSection) AppMotion.enter(sections[index])
        currentSection = index
    }

    override fun onSaveInstanceState(outState: Bundle) {
        if (!::binding.isInitialized) { super.onSaveInstanceState(outState); return }
        outState.putString("gifSource", gifSource?.toString())
        outState.putString("gifSourceName", gifSourceName)
        outState.putString("compatibilitySource", compatibilitySource?.toString())
        outState.putString("compatibilitySourceName", compatibilitySourceName)
        outState.putString("upscaleSource", upscaleSource?.toString())
        outState.putString("upscaleSourceName", upscaleSourceName)
        outState.putInt("selectedTab", binding.bottomNavigation.selectedItemId)
        outState.putInt("queueFilter", binding.queueFilters.checkedChipId)
        super.onSaveInstanceState(outState)
    }

    private fun setupSpinners() {
        binding.modeSpinner.adapter = adapter(listOf("Vídeo + áudio", "Apenas vídeo", "Apenas áudio"))
        binding.qualitySpinner.adapter = adapter(listOf("Melhor disponível"))
        binding.formatSpinner.adapter = adapter(listOf("MP4", "MKV", "WEBM"))
        binding.gifResolutionSpinner.adapter = adapter(listOf("360p", "480p", "720p", "Original"))
        binding.gifResolutionSpinner.setSelection(1)
        binding.gifFpsSpinner.adapter = adapter(listOf("10 fps", "15 fps", "20 fps", "24 fps", "30 fps"))
        binding.gifFpsSpinner.setSelection(1)
        binding.upscaleModelSpinner.adapter = adapter(
            listOf(UpscaleModel.ANIME_VIDEO_V3.displayName, UpscaleModel.REAL_CUGAN.displayName),
        )
        binding.upscaleModelSpinner.setSelection(0)
        binding.upscaleScaleSpinner.adapter = adapter(listOf("2x", "3x", "4x"))
        binding.upscaleScaleSpinner.setSelection(0)
        binding.upscaleModelDescription.text = UpscaleModel.ANIME_VIDEO_V3.description
        binding.upscaleModelSpinner.onItemSelectedListener = SimpleItemSelectedListener { position ->
            binding.upscaleModelDescription.text = upscaleModels[position].description
        }
        binding.modeSpinner.onItemSelectedListener = SimpleItemSelectedListener { position ->
            binding.qualitySpinner.isEnabled = position != 2
            binding.formatSpinner.adapter = adapter(
                if (position == 2) listOf("MP3", "M4A", "OPUS")
                else listOf("MP4", "MKV", "WEBM"),
            )
        }
    }

    private fun setupActions() {
        binding.settingsButton.setOnClickListener {
            val preferences = getSharedPreferences("studio_visuals", MODE_PRIVATE)
            val paused = preferences.getBoolean("motion_paused", false)
            MaterialAlertDialogBuilder(this)
                .setTitle("Configurações")
                .setItems(arrayOf("Alterar nome", if (paused) "Ativar animações" else "Desativar animações")) { _, which ->
                    if (which == 0) startActivity(Intent(this, WelcomeActivity::class.java).putExtra("edit_name", true))
                    else {
                        preferences.edit().putBoolean("motion_paused", !paused).apply()
                        if (!paused) listOf(binding.downloadSection, binding.gifSection,
                            binding.compatibilitySection, binding.upscaleSection, binding.queueSection).forEach(AppMotion::reset)
                    }
                }.show()
        }
        binding.emptyQueueAction.setOnClickListener {
            if (latestJobs.isEmpty()) binding.bottomNavigation.selectedItemId = R.id.navigationDownload
            else binding.queueFilters.check(R.id.filterAll)
        }
        binding.urlInput.doAfterTextChanged {
            val current = it?.toString()?.trim().orEmpty()
            binding.linkStatus.text = if (android.util.Patterns.WEB_URL.matcher(current).matches()) "Link reconhecido · pronto para analisar" else "1  Link   →   2  Qualidade   →   3  Download"
            if (analyzedUrl != null && current != analyzedUrl) {
                analyzedUrl = null
                resetVideoPreview()
            }
        }
        binding.urlInput.setOnEditorActionListener { _, actionId, _ ->
            if (actionId == EditorInfo.IME_ACTION_GO) {
                analyzeUrl()
                true
            } else {
                false
            }
        }
        binding.pasteButton.setOnClickListener { pasteUrlFromClipboard() }
        binding.cancelAnalysisButton.setOnClickListener { analysisTask?.cancel() }
        binding.viewQueueButton.setOnClickListener { binding.bottomNavigation.selectedItemId = R.id.navigationQueue }
        binding.analyzeButton.setOnClickListener { analyzeUrl() }
        binding.enqueueButton.setOnClickListener { enqueueDownload() }
        binding.pickGifSourceButton.setOnClickListener { pickGif.launch(arrayOf("video/*")) }
        binding.pickCompatibilitySourceButton.setOnClickListener {
            pickCompatibility.launch(arrayOf("video/*"))
        }
        binding.createGifButton.setOnClickListener { enqueueGif() }
        binding.convertButton.setOnClickListener { enqueueCompatibility() }
        binding.pickUpscaleSourceButton.setOnClickListener {
            pickUpscale.launch(arrayOf("video/*"))
        }
        binding.startUpscaleButton.setOnClickListener { prepareUpscale() }
        binding.updateButton.setOnClickListener {
            lifecycleScope.launch { checkForUpdates(showWhenCurrent = true) }
        }
        binding.clearQueueButton.setOnClickListener {
            MaterialAlertDialogBuilder(this).setTitle("Limpar histórico?")
                .setMessage("Os arquivos salvos em Downloads serão mantidos.")
                .setNegativeButton("Voltar", null).setPositiveButton("Limpar") { _, _ ->
                    lifecycleScope.launch(Dispatchers.IO) {
                        latestJobs.filter { it.status in setOf("completed", "failed", "cancelled") }.forEach {
                            File(noBackupFilesDir, "jobs/${it.id}").deleteRecursively()
                        }
                        dao.clearFinished()
                    }
                }.show()
        }
    }

    private fun pasteUrlFromClipboard() {
        val clipboard = getSystemService(ClipboardManager::class.java)
        val clip = clipboard?.primaryClip
        val text = if (clip != null && clip.itemCount > 0) {
            clip.getItemAt(0).coerceToText(this)?.toString()?.trim().orEmpty()
        } else {
            ""
        }
        if (text.isBlank()) {
            toast("A área de transferência está vazia.")
            return
        }
        binding.urlInput.error = null
        binding.urlInput.setText(text)
        binding.urlInput.setSelection(text.length)
    }

    private fun setupQueue() {
        jobsAdapter = JobAdapter(
            onCancel = { DownloadService.cancel(this, it.id) },
            onOpen = ::openJob,
            onFallback = ::confirmServerFallback,
            onShowError = { job ->
                mostrarDetalheDoErro("Erro em \"${job.title}\"", job.errorDetail.orEmpty())
            },
            onRetry = ::retryJob,
            onShare = ::shareJob,
        )
        recentAdapter = JobAdapter(
            onCancel = { DownloadService.cancel(this, it.id) }, onOpen = ::openJob,
            onFallback = ::confirmServerFallback,
            onShowError = { mostrarDetalheDoErro("Detalhes da tarefa", it.errorDetail.orEmpty()) },
            onRetry = ::retryJob, onShare = ::shareJob,
        )
        binding.recentJobs.layoutManager = LinearLayoutManager(this)
        binding.recentJobs.adapter = recentAdapter
        binding.recentJobs.isNestedScrollingEnabled = false
        binding.recentJobs.itemAnimator = null
        binding.jobsList.layoutManager = LinearLayoutManager(this)
        binding.jobsList.adapter = jobsAdapter
        (binding.jobsList.itemAnimator as? SimpleItemAnimator)?.supportsChangeAnimations = false
        binding.queueFilters.setOnCheckedStateChangeListener { _, ids ->
            queueFilter = when (ids.firstOrNull()) {
                R.id.filterActive -> QueueFilter.ACTIVE
                R.id.filterCompleted -> QueueFilter.COMPLETED
                R.id.filterFailed -> QueueFilter.FAILED
                else -> QueueFilter.ALL
            }
            renderQueue()
        }
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                dao.observeAll().collect { jobs ->
                    latestJobs = jobs
                    renderQueue()
                }
            }
        }
    }

    private fun renderQueue() {
        recentAdapter.submitList(latestJobs.take(3))
        binding.recentEmpty.visibility = if (latestJobs.isEmpty()) View.VISIBLE else View.GONE
        listOf(binding.filterAll, binding.filterActive, binding.filterCompleted, binding.filterFailed).forEach { chip ->
            chip.setTypeface(null, if (chip.isChecked) Typeface.BOLD else Typeface.NORMAL)
        }
        val visible = latestJobs.filter(queueFilter::accepts)
        jobsAdapter.submitList(visible)
        val active = latestJobs.count(QueueFilter.ACTIVE::accepts)
        val completed = latestJobs.count(QueueFilter.COMPLETED::accepts)
        val failed = latestJobs.count(QueueFilter.FAILED::accepts)
        binding.queueSummary.visibility = if (latestJobs.isEmpty()) View.GONE else View.VISIBLE
        binding.queueSummary.text = "$active ativos · $completed prontos · $failed falhas"
        binding.clearQueueButton.isEnabled = latestJobs.any { !QueueFilter.ACTIVE.accepts(it) }
        binding.emptyQueueState.visibility = if (visible.isEmpty()) View.VISIBLE else View.GONE
        binding.emptyQueueAction.text = if (latestJobs.isEmpty()) "Baixar vídeo" else "Ver todas as tarefas"
        binding.emptyQueueText.text = if (latestJobs.isEmpty())
            "Nenhum arquivo na fila."
            else "Nenhuma tarefa neste filtro."
        binding.jobsList.visibility = if (visible.isEmpty()) View.GONE else View.VISIBLE
        if (active > 0) {
            binding.bottomNavigation.getOrCreateBadge(R.id.navigationQueue).apply {
                number = active
                backgroundColor = ContextCompat.getColor(this@MainActivity, R.color.red_brand)
                badgeTextColor = android.graphics.Color.WHITE
            }
        } else binding.bottomNavigation.removeBadge(R.id.navigationQueue)
    }

    private fun analyzeUrl() {
        if (analysisRunning) return
        if (remoteConfig.requiresUpdate) {
            lifecycleScope.launch { checkForUpdates(true) }
            return
        }
        val analysisStarted = SystemClock.elapsedRealtime()
        val typed = binding.urlInput.text?.toString()?.trim().orEmpty()
        val parsed = runCatching { Uri.parse(typed) }.getOrNull()
        if (parsed?.scheme !in setOf("http", "https") || parsed?.host.isNullOrBlank()) {
            binding.urlInput.error = "Cole um link válido."
            return
        }
        // Canoniza antes de tudo: a análise, o cache e o job da fila precisam
        // usar exatamente o mesmo link, aqui e no servidor.
        val url = LocalTools.canonicalLink(typed)
        binding.urlInput.error = null
        // Limpa o campo assim que a busca começa. Precisa ser aqui, antes de
        // `analyzedUrl` ser preenchido: o observador do campo descarta a análise
        // quando o texto muda, e limpar depois apagaria o resultado da busca.
        binding.urlInput.text?.clear()
        analysisRunning = true
        binding.urlInput.isEnabled = false
        binding.pasteButton.isEnabled = false
        binding.analyzeButton.isEnabled = false
        binding.enqueueButton.isEnabled = false
        binding.analyzeProgress.visibility = View.VISIBLE
        binding.cancelAnalysisButton.visibility = View.VISIBLE
        binding.analyzeButton.text = "Analisando…"
        binding.videoPreview.visibility = View.GONE
        binding.downloadOptions.visibility = View.GONE
        binding.videoInfoText.text = "Buscando vídeo..."
        binding.analysisErrorButton.visibility = View.GONE
        analysisTask = lifecycleScope.launch {
            try {
                val cache = VideoInfoCache.from(applicationContext)
                val cachedInfo = readCachedVideoInfo(cache, url)
                if (cachedInfo != null) {
                    mostrarInfoLocal(url, cachedInfo)
                    Log.i(
                        TAG,
                        "analysis_timing source=cache total_ms=" +
                            "${SystemClock.elapsedRealtime() - analysisStarted}",
                    )
                    return@launch
                }
                raceVideoAnalysis(url, cache, analysisStarted)
            } catch (error: CancellationException) {
                binding.urlInput.setText(url)
                binding.videoInfoText.text = if (error is kotlinx.coroutines.TimeoutCancellationException)
                    "A análise demorou demais. Tente novamente." else "Análise cancelada. O link foi mantido."
                throw error
            } catch (error: Throwable) {
                if (error is VirtualMachineError || error is ThreadDeath) throw error
                Log.e(TAG, "analysis_failed type=${error.javaClass.simpleName}")
                Log.i(
                    TAG,
                    "analysis_timing source=local_failed total_ms=" +
                        "${SystemClock.elapsedRealtime() - analysisStarted}",
                )
                analyzedUrl = null
                // Devolve o link quando as duas buscas falham: sem isso o
                // usuário teria que copiar e colar novamente para repetir.
                binding.urlInput.setText(url)
                binding.videoInfoText.text = friendlyAnalysisError(error)
                val detalhe = detalheDaBusca(url, error)
                binding.analysisErrorButton.visibility = View.VISIBLE
                binding.analysisErrorButton.setOnClickListener {
                    mostrarDetalheDoErro("Erro na busca", detalhe)
                }
            } finally {
                analysisRunning = false
                binding.urlInput.isEnabled = true
                binding.pasteButton.isEnabled = true
                binding.analyzeButton.isEnabled = true
                binding.analyzeProgress.visibility = View.GONE
                binding.cancelAnalysisButton.visibility = View.GONE
                binding.analyzeButton.text = "Buscar vídeo"
            }
        }
    }

    private suspend fun raceVideoAnalysis(
        url: String,
        cache: VideoInfoCache,
        analysisStarted: Long,
    ) = supervisorScope {
        val lease = VideoInfoWarmup.begin(url)
        val local = async {
            try {
                val info = if (lease.isOwner) {
                    extractLocalVideoInfo(url, cache)
                } else {
                    check(lease.await(ANALYSIS_TIMEOUT_MS)) { "A análise local não terminou a tempo" }
                    readCachedVideoInfo(cache, url)
                        ?: error("A análise compartilhada não gerou metadados")
                }
                AnalysisResult.LocalSuccess(info)
            } catch (error: CancellationException) {
                currentCoroutineContext().ensureActive()
                AnalysisResult.LocalFailure(error)
            } catch (error: Throwable) {
                if (error is VirtualMachineError || error is ThreadDeath) throw error
                AnalysisResult.LocalFailure(error)
            } finally {
                lease.finish()
            }
        }
        val remote = async {
            try {
                if (!remoteConfig.serverFallbackEnabled) {
                    error("Busca pela API desativada")
                }
                AnalysisResult.RemoteSuccess(fetchRemoteVideoInfo(url, analysisStarted))
            } catch (error: CancellationException) {
                currentCoroutineContext().ensureActive()
                AnalysisResult.RemoteFailure(error)
            } catch (error: Throwable) {
                if (error is VirtualMachineError || error is ThreadDeath) throw error
                Log.w(TAG, "analysis_api_failed type=${error.javaClass.simpleName}")
                AnalysisResult.RemoteFailure(error)
            }
        }

        when (val first = select<AnalysisResult> {
            local.onAwait { it }
            remote.onAwait { it }
        }) {
            is AnalysisResult.LocalSuccess -> {
                remote.cancel()
                mostrarInfoLocal(url, first.info)
                binding.analyzeButton.isEnabled = true
                binding.analyzeProgress.visibility = View.GONE
                Log.i(
                    TAG,
                    "analysis_timing source=local total_ms=" +
                        "${SystemClock.elapsedRealtime() - analysisStarted}",
                )
            }
            is AnalysisResult.RemoteSuccess -> {
                mostrarInfoRemota(url, first.info)
                // O resultado já pode ser usado enquanto a extração local
                // termina o cache que será reaproveitado pelo download.
                binding.analyzeButton.isEnabled = true
                binding.analyzeProgress.visibility = View.GONE
                when (val warmed = local.await()) {
                    is AnalysisResult.LocalSuccess -> Log.i(
                        TAG,
                        "analysis_cache_warmup success=true total_ms=" +
                            "${SystemClock.elapsedRealtime() - analysisStarted}",
                    )
                    is AnalysisResult.LocalFailure -> Log.w(
                        TAG,
                        "analysis_cache_warmup success=false " +
                            "type=${warmed.error.javaClass.simpleName}",
                    )
                    else -> Unit
                }
            }
            is AnalysisResult.LocalFailure -> when (val fallback = remote.await()) {
                is AnalysisResult.RemoteSuccess -> mostrarInfoRemota(url, fallback.info)
                else -> throw first.error
            }
            is AnalysisResult.RemoteFailure -> when (val fallback = local.await()) {
                is AnalysisResult.LocalSuccess -> {
                    mostrarInfoLocal(url, fallback.info)
                    Log.i(
                        TAG,
                        "analysis_timing source=local_after_api total_ms=" +
                            "${SystemClock.elapsedRealtime() - analysisStarted}",
                    )
                }
                is AnalysisResult.LocalFailure -> throw fallback.error
                else -> throw first.error
            }
        }
    }

    private suspend fun readCachedVideoInfo(cache: VideoInfoCache, url: String): VideoInfo? {
        val previewDirectory = File(cacheDir, "analysis-preview").apply { mkdirs() }
        val previewCopy = File(previewDirectory, "${UUID.randomUUID()}.info.json")
        return try {
            withContext(Dispatchers.IO) {
                runCatching {
                    cache.copyFreshTo(url, previewCopy)?.let {
                        videoInfoMapper.readValue(it, VideoInfo::class.java)
                    }
                }.getOrElse {
                    cache.invalidate(url)
                    null
                }
            }
        } finally {
            previewCopy.delete()
            previewDirectory.delete()
        }
    }

    private suspend fun extractLocalVideoInfo(url: String, cache: VideoInfoCache): VideoInfo {
        val staging = cache.createStaging(url)
        return try {
            withTimeout(ANALYSIS_TIMEOUT_MS) {
                // Cookies e inicialização não dependem um do outro. Fazer os
                // dois em paralelo reduz o custo do primeiro uso.
                val cookies = async(Dispatchers.IO) {
                    val started = SystemClock.elapsedRealtime()
                    LocalTools.cookiesFile(applicationContext, app.api).also {
                        Log.i(
                            TAG,
                            "analysis_phase cookies_ms=" +
                                "${SystemClock.elapsedRealtime() - started}",
                        )
                    }
                }
                val initStarted = SystemClock.elapsedRealtime()
                withContext(Dispatchers.IO) {
                    // A análise usa somente o yt-dlp; FFmpeg não ajuda aqui.
                    LocalTools.ensureYoutubeDlInitialized(applicationContext)
                }
                Log.i(
                    TAG,
                    "analysis_phase ytdlp_init_ms=" +
                        "${SystemClock.elapsedRealtime() - initStarted}",
                )
                val cookiesPath = cookies.await()
                val extractStarted = SystemClock.elapsedRealtime()
                val info = try {
                    extrairComYoutubeDl(url, staging, cache, cookiesPath)
                } catch (error: Throwable) {
                    if (error is CancellationException || error is VirtualMachineError ||
                        error is ThreadDeath || error is LinkageError
                    ) throw error
                    // Mesma recuperação que o download já fazia: extrator
                    // defasado não é link inválido. Sem isto, a busca apenas
                    // falhava e o yt-dlp do APK nunca era renovado.
                    val detalhe = (error.message ?: error.cause?.message).orEmpty()
                    if (!LocalTools.isStaleExtractorFailure(detalhe)) throw error
                    withContext(Dispatchers.IO) {
                        if (!LocalTools.refreshYoutubeDl(applicationContext)) throw error
                        cache.discard(staging)
                    }
                    Log.i(TAG, "analysis_retry_after_update")
                    extrairComYoutubeDl(url, staging, cache, cookiesPath)
                }
                Log.i(
                    TAG,
                    "analysis_phase extraction_ms=" +
                        "${SystemClock.elapsedRealtime() - extractStarted}",
                )
                info
            }
        } finally {
            cache.discard(staging)
        }
    }

    private suspend fun extrairComYoutubeDl(
        url: String,
        staging: VideoInfoCache.Staging,
        cache: VideoInfoCache,
        cookiesPath: String?,
    ): VideoInfo = runInterruptible(Dispatchers.IO) {
        val processId = "analysis-${UUID.randomUUID()}"
        try {
            val request = YoutubeDLRequest(url)
                .addOption("--skip-download")
                .addOption("--write-info-json")
                .addOption("--no-write-playlist-metafiles")
                .addOption("--no-playlist")
                .addOption("--no-warnings")
                .addOption("--js-runtimes", LocalTools.jsRuntimeOption(applicationContext))
                .addOption("--user-agent", LocalTools.BROWSER_USER_AGENT)
                .addOption("--add-header", LocalTools.ACCEPT_LANGUAGE_HEADER)
                .addOption("--extractor-args", LocalTools.YOUTUBE_EXTRACTOR_ARGS)
                .addOption("--socket-timeout", 20)
                .addOption("--retries", 2)
                .addOption("-o", staging.outputTemplate)
                .apply {
                    if (LocalTools.isTikTokLink(url)) {
                        addOption("--referer", LocalTools.TIKTOK_REFERER)
                    }
                }
                .apply { cookiesPath?.let { addOption("--cookies", it) } }
            LocalTools.withYoutubeDl { YoutubeDL.getInstance().execute(request, processId) }
            val infoFile = cache.readyForParsing(staging)
                ?: error("Metadados locais vazios ou grandes demais")
            videoInfoMapper.readValue(infoFile, VideoInfo::class.java)
                .also { cache.commit(url, staging) }
        } finally {
            // O timeout/cancelamento não pode deixar Python/QJS e seus filhos
            // consumindo memória em segundo plano.
            LocalProcessKiller.destroyYoutubeDl(processId)
        }
    }

    private fun mostrarInfoLocal(url: String, info: VideoInfo) {
        analyzedUrl = url
        analyzedTitle = info.title?.take(160).orEmpty().ifBlank { "Vídeo" }
        val heights = info.formats.orEmpty()
            .filter { it.height > 0 && !it.vcodec.equals("none", true) }
            .map { it.height }.distinct().sortedDescending()
        mostrarVideo(
            heights,
            info.uploader,
            info.duration.toInt(),
            info.thumbnail,
        )
    }

    /** Preenche o card do vídeo, venha ele do aparelho ou da API. */
    private fun mostrarVideo(
        heights: List<Int>,
        uploader: String?,
        duration: Int,
        thumbnail: String?,
    ) {
        qualityHeights = listOf(null) + heights
        binding.qualitySpinner.adapter = adapter(
            listOf("Melhor disponível") + heights.map { "${it}p" },
        )
        binding.videoTitleText.text = analyzedTitle
        binding.videoMetaText.text = buildList {
            uploader?.takeIf(String::isNotBlank)?.let(::add)
            if (duration > 0) add(formatDuration(duration))
        }.joinToString(" · ").ifBlank { "Vídeo encontrado" }
        binding.videoInfoText.text = ""
        binding.analysisErrorButton.visibility = View.GONE
        binding.videoPreview.visibility = View.VISIBLE
        binding.downloadOptions.visibility = View.VISIBLE
        AppMotion.enter(binding.videoPreview)
        AppMotion.enter(binding.downloadOptions)
        binding.enqueueButton.isEnabled = true
        loadThumbnail(thumbnail)
    }

    private fun mostrarInfoRemota(url: String, info: RemoteVideoInfo) {
        analyzedUrl = url
        analyzedTitle = info.title.take(160)
        mostrarVideo(info.heights, info.uploader, info.duration, info.thumbnail)
    }

    private suspend fun fetchRemoteVideoInfo(url: String, overallStarted: Long): RemoteVideoInfo {
        val apiStarted = SystemClock.elapsedRealtime()
        val info = withTimeout(ANALYSIS_TIMEOUT_MS) { app.api.videoInfo(url) }
        Log.i(
            TAG,
            "analysis_timing source=api request_ms=" +
                "${SystemClock.elapsedRealtime() - apiStarted} " +
                "total_ms=${SystemClock.elapsedRealtime() - overallStarted}",
        )
        return info
    }

    private fun enqueueDownload() {
        val url = analyzedUrl ?: return
        val mode = listOf("va", "v", "a")[binding.modeSpinner.selectedItemPosition]
        val height = if (mode == "a") null else qualityHeights
            .getOrNull(binding.qualitySpinner.selectedItemPosition)
        val format = binding.formatSpinner.selectedItem.toString().lowercase()
        enqueue(
            DownloadJob(
                id = UUID.randomUUID().toString(),
                // O download continua local mesmo quando a busca precisou da
                // API: a recusa do YouTube é intermitente e o download costuma
                // passar. Se falhar, a fila oferece o reenvio pelo servidor.
                type = if (remoteConfig.processingMode == "server") {
                    DownloadJob.TYPE_SERVER_DOWNLOAD
                } else {
                    DownloadJob.TYPE_DOWNLOAD
                },
                url = url,
                title = analyzedTitle,
                mode = mode,
                height = height,
                videoFormat = if (mode == "a") "mp4" else format,
                audioFormat = if (mode == "a") format else "mp3",
            ),
            binding.downloadMessage,
        )
    }

    private fun enqueueGif() {
        val uri = gifSource ?: run {
            binding.gifMessage.text = "Escolha um vídeo primeiro."
            return
        }
        val start = binding.gifStartInput.text?.toString()?.toDoubleOrNull() ?: -1.0
        val end = binding.gifEndInput.text?.toString()?.toDoubleOrNull() ?: -1.0
        if (start < 0 || end <= start || end - start > 30) {
            binding.gifMessage.text = "Escolha um trecho válido de até 30 segundos."
            return
        }
        val resolution = binding.gifResolutionSpinner.selectedItem.toString().lowercase()
        val fps = binding.gifFpsSpinner.selectedItem.toString().substringBefore(' ').toInt()
        val options = GifOptions(start, end, resolution = resolution, fps = fps)
        enqueue(
            DownloadJob(
                id = UUID.randomUUID().toString(),
                type = DownloadJob.TYPE_GIF,
                sourceUri = uri.toString(),
                title = gifSourceName.substringBeforeLast('.'),
                optionsJson = Gson().toJson(options),
            ),
            binding.gifMessage,
        )
    }

    private fun enqueueCompatibility() {
        val uri = compatibilitySource ?: run {
            binding.compatibilityMessage.text = "Escolha um vídeo primeiro."
            return
        }
        enqueue(
            DownloadJob(
                id = UUID.randomUUID().toString(),
                type = DownloadJob.TYPE_COMPATIBILITY,
                sourceUri = uri.toString(),
                title = compatibilitySourceName.substringBeforeLast('.'),
            ),
            binding.compatibilityMessage,
        )
    }

    private fun inspectUpscaleHardware() {
        binding.upscaleHardwareText.text = "Verificando hardware..."
        lifecycleScope.launch {
            val capabilities = withContext(Dispatchers.IO) {
                DeviceCapabilities.inspect(this@MainActivity)
            }
            binding.upscaleHardwareText.text = if (capabilities.canUseVulkan) {
                "Vulkan disponível · ${capabilities.gpu.deviceName.ifBlank { "GPU compatível" }} · " +
                    "${capabilities.availableRamMb} MB de RAM disponíveis"
            } else {
                "Vulkan indisponível; o aplicativo tentará CPU · " +
                    "${capabilities.availableRamMb} MB de RAM disponíveis"
            }
        }
    }

    private fun prepareUpscale() {
        val uri = upscaleSource ?: run {
            binding.upscaleMessage.text = "Escolha um vídeo primeiro."
            return
        }
        if (!remoteConfig.upscaleEnabled) {
            binding.upscaleMessage.text = "O Upscale ainda não foi ativado pelo servidor."
            return
        }
        if (BuildConfig.VERSION_CODE < remoteConfig.upscaleMinimumVersionCode) {
            binding.upscaleMessage.text = "Atualize o Slucss System para usar o Upscale."
            return
        }
        val model = upscaleModels[binding.upscaleModelSpinner.selectedItemPosition]
        val scale = binding.upscaleScaleSpinner.selectedItemPosition + 2
        val remoteEngine = remoteConfig.upscaleEngineVersion
        val remoteModel = remoteConfig.upscaleModelVersion(model.apiValue)
        if ((remoteEngine.isNotBlank() && remoteEngine != UpscaleVersions.ENGINE_VERSION) ||
            (remoteModel.isNotBlank() && remoteModel != model.modelVersion)
        ) {
            binding.upscaleMessage.text = "Atualize o Slucss System para usar estes modelos."
            return
        }
        binding.startUpscaleButton.isEnabled = false
        binding.upscaleMessage.text = "Analisando vídeo..."
        lifecycleScope.launch {
            try {
                val probe = withContext(Dispatchers.IO) {
                    VideoUpscalePipeline.probe(this@MainActivity, uri)
                }
                if (probe.width <= 0 || probe.height <= 0) {
                    error("O Android não conseguiu ler as dimensões deste vídeo.")
                }
                val continueAction = {
                    enqueueUpscale(uri, model, scale)
                    Unit
                }
                if (probe.durationMillis > 10_000) {
                    val modelWarning = if (model == UpscaleModel.REAL_CUGAN) {
                        "\n\nO Real-CUGAN prioriza qualidade e pode demorar significativamente mais."
                    } else {
                        ""
                    }
                    MaterialAlertDialogBuilder(this@MainActivity)
                        .setTitle("Vídeo com mais de 10 segundos")
                        .setMessage(
                            "Este vídeo possui mais de 10 segundos.\n\n" +
                                "O Upscale pode levar vários minutos e consumir bastante bateria " +
                                "dependendo da resolução, FPS, modelo e desempenho do aparelho." +
                                modelWarning,
                        )
                        .setNegativeButton("Cancelar", null)
                        .setPositiveButton("Continuar") { _, _ -> continueAction() }
                        .show()
                } else {
                    continueAction()
                }
            } catch (error: Exception) {
                binding.upscaleMessage.text = error.message ?: "Não foi possível analisar o vídeo."
            } finally {
                binding.startUpscaleButton.isEnabled = true
            }
        }
    }

    private fun enqueueUpscale(uri: Uri, model: UpscaleModel, scale: Int) {
        val options = UpscaleOptions(
            model = model.apiValue,
            scale = scale,
            outputFormat = "auto",
            modelVersion = model.modelVersion,
        )
        enqueue(
            DownloadJob(
                id = UUID.randomUUID().toString(),
                type = DownloadJob.TYPE_UPSCALE,
                sourceUri = uri.toString(),
                title = upscaleSourceName.substringBeforeLast('.'),
                optionsJson = Gson().toJson(options),
            ),
            binding.upscaleMessage,
        )
    }

    private fun enqueue(job: DownloadJob, messageView: android.widget.TextView) {
        if (remoteConfig.requiresUpdate) {
            messageView.text = "Atualize o aplicativo para continuar."
            lifecycleScope.launch { checkForUpdates(true) }
            return
        }
        if (enqueuePending || !legacyStorageAvailable()) return
        enqueuePending = true
        lifecycleScope.launch {
            try {
                withContext(Dispatchers.IO) { dao.upsert(job) }
                DownloadService.start(this@MainActivity)
                messageView.text = "Adicionado à fila."
                binding.queueFilters.check(R.id.filterAll)
                binding.bottomNavigation.selectedItemId = R.id.navigationQueue
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) { messageView.text = "Não foi possível iniciar. Tente novamente." }
            finally { enqueuePending = false }
        }
    }

    private suspend fun checkForUpdates(showWhenCurrent: Boolean) {
        binding.updateButton.isEnabled = false
        binding.updateButton.text = "Verificando..."
        try {
            val manifest = app.api.androidManifest()
            remoteConfig.update(manifest)
            com.xard.ytsystem.update.ReleaseNotifier.notify(this, manifest)
            if (manifest.latestVersionCode > BuildConfig.VERSION_CODE) {
                showUpdateDialog(manifest, ReleasePolicy.required(manifest, BuildConfig.VERSION_CODE))
            } else if (manifest.latestVersionCode == BuildConfig.VERSION_CODE &&
                ReleaseAnnouncement.showOnlineIfNeeded(this)) {
                // The online artwork is shown once, after the server confirms this release.
            } else if (showWhenCurrent) {
                toast("Você já está usando a versão mais recente.")
            }
        } catch (error: Exception) {
            if (showWhenCurrent) toast(error.message ?: "Não foi possível verificar atualizações.")
        } finally {
            binding.updateButton.isEnabled = true
            binding.updateButton.text = "Atualizar"
        }
    }

    private fun showUpdateDialog(manifest: AndroidManifest, required: Boolean) {
        ReleaseAnnouncement.showUpdate(this, manifest, required) { startUpdate(manifest, required) }
    }

    private fun startUpdate(manifest: AndroidManifest, required: Boolean) {
        val cached = pendingUpdateApk
        if (cached?.isFile == true && pendingUpdateVersionCode == manifest.latestVersionCode) {
            pendingUpdateRequired = required
            showUpdateReadyDialog(required)
            return
        }
        binding.updateButton.isEnabled = false
        val progressDialog = MaterialAlertDialogBuilder(this)
            .setTitle("Baixando atualização")
            .setMessage("Preparando o APK...")
            .setCancelable(false)
            .create()
        progressDialog.show()
        lifecycleScope.launch {
            try {
                val apk = updater.download(manifest) { percent ->
                    runOnUiThread {
                        binding.updateButton.text = "$percent%"
                        progressDialog.setMessage("Baixando... $percent%")
                    }
                }
                progressDialog.dismiss()
                pendingUpdateApk = apk
                pendingUpdateVersionCode = manifest.latestVersionCode
                pendingUpdateRequired = required
                showUpdateReadyDialog(required)
            } catch (error: CancellationException) {
                progressDialog.dismiss()
                throw error
            } catch (error: Exception) {
                progressDialog.dismiss()
                toast(error.message ?: "Não foi possível baixar a atualização.")
                if (required) showUpdateDialog(manifest, true)
            } finally {
                binding.updateButton.isEnabled = true
                binding.updateButton.text = "Atualizar"
            }
        }
    }

    private fun showUpdateReadyDialog(required: Boolean) {
        if (pendingUpdateApk?.isFile != true) return
        MaterialAlertDialogBuilder(this)
            .setTitle("Atualização verificada")
            .setMessage(
                "O APK passou pelas verificações de hash, pacote, versão e certificado. " +
                    "O Android ainda pedirá a confirmação final.",
            )
            .setCancelable(!required)
            .setNegativeButton(if (required) null else "Depois", null)
            .setPositiveButton("Atualizar agora") { _, _ -> openPendingUpdate() }
            .show()
    }

    private fun openPendingUpdate() {
        val apk = pendingUpdateApk?.takeIf(File::isFile) ?: return
        if (updater.needsUnknownSourcesPermission()) {
            waitingForUpdatePermission = true
            updater.requestUnknownSourcesPermission()
            toast("Permita a atualização; depois o Android pedirá a confirmação final.")
            return
        }
        updater.openInstaller(apk)
    }

    private fun openJob(job: DownloadJob) {
        val uri = job.outputUri?.let(Uri::parse) ?: return
        try {
            startActivity(Intent(Intent.ACTION_VIEW).apply {
                setDataAndType(uri, job.outputMime ?: "*/*")
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            })
        } catch (_: Exception) {
            runCatching { startActivity(Intent(DownloadManager.ACTION_VIEW_DOWNLOADS)) }
                .onFailure { toast("O arquivo está em Downloads/Slucss.") }
        }
    }

    private fun confirmServerFallback(job: DownloadJob) {
        if (!remoteConfig.serverFallbackEnabled) {
            toast("O processamento pelo servidor não está disponível agora.")
            return
        }
        MaterialAlertDialogBuilder(this)
            .setTitle("Tentar pelo servidor?")
            .setMessage(
                "Esta é uma exceção ao modo local. O servidor baixará e processará o vídeo, " +
                    "e depois o arquivo pronto será enviado ao celular.",
            )
            .setNegativeButton("Cancelar", null)
            .setPositiveButton("Continuar") { _, _ ->
                enqueue(
                    job.copy(
                        id = UUID.randomUUID().toString(),
                        type = DownloadJob.TYPE_SERVER_DOWNLOAD,
                        operationId = "",
                        status = DownloadJob.STATUS_QUEUED,
                        stage = "queued",
                        progress = 0f,
                        downloadedBytes = 0,
                        totalBytes = 0,
                        speedBytes = 0,
                        etaSeconds = -1,
                        message = "Aguardando na fila.",
                        outputUri = null,
                        outputMime = null,
                        outputName = null,
                        error = null,
                        fallbackEligible = false,
                        createdAt = System.currentTimeMillis(),
                        updatedAt = System.currentTimeMillis(),
                    ),
                    binding.downloadMessage,
                )
            }
            .show()
    }

    private fun retainReadPermission(uri: Uri) {
        runCatching {
            contentResolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
    }

    private fun displayName(uri: Uri): String? = contentResolver.query(
        uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null,
    )?.use { cursor ->
        if (cursor.moveToFirst()) cursor.getString(0) else null
    }

    private fun requestRuntimePermissions() {
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) requestNotification.launch(Manifest.permission.POST_NOTIFICATIONS)
    }

    private fun legacyStorageAvailable(): Boolean {
        if (Build.VERSION.SDK_INT > 28) return true
        val granted = ContextCompat.checkSelfPermission(
            this, Manifest.permission.WRITE_EXTERNAL_STORAGE,
        ) == PackageManager.PERMISSION_GRANTED
        if (!granted) requestLegacyStorage.launch(Manifest.permission.WRITE_EXTERNAL_STORAGE)
        return granted
    }

    private fun adapter(values: List<String>) = ArrayAdapter(
        this, R.layout.item_spinner_selected, values,
    ).also { it.setDropDownViewResource(R.layout.item_spinner_dropdown) }

    /** Mesmo formato do detalhe gravado nas tarefas, para a falha da busca. */
    private fun detalheDaBusca(url: String, error: Throwable): String = buildString {
        appendLine("Slucss ${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE})")
        appendLine(
            "Android ${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT}) " +
                "${Build.SUPPORTED_ABIS.firstOrNull()}",
        )
        appendLine("Etapa: busca")
        appendLine("Link: $url")
        appendLine("Erro: ${error.javaClass.simpleName}: ${error.message.orEmpty()}")
        error.cause?.let { appendLine("Causa: ${it.javaClass.simpleName}: ${it.message.orEmpty()}") }
    }.let(SafeDiagnostics::redact).trim().take(4000)

    /**
     * Mostra o texto técnico da falha para a pessoa copiar e mandar no suporte.
     *
     * Sem isto, o motivo real só existia no logcat — que quem tem apenas o
     * aparelho na mão não consegue ler. O texto é selecionável e o botão copia
     * tudo de uma vez.
     */
    private fun mostrarDetalheDoErro(titulo: String, detalhe: String) {
        if (detalhe.isBlank()) return
        val texto = TextView(this).apply {
            text = detalhe
            setTextIsSelectable(true)
            typeface = Typeface.MONOSPACE
            textSize = 12f
            val margem = (16 * resources.displayMetrics.density).toInt()
            setPadding(margem, margem / 2, margem, 0)
        }
        val rolagem = ScrollView(this).apply { addView(texto) }
        MaterialAlertDialogBuilder(this)
            .setTitle(titulo)
            .setView(rolagem)
            .setPositiveButton("Copiar") { _, _ ->
                val clipboard = getSystemService(ClipboardManager::class.java)
                clipboard?.setPrimaryClip(ClipData.newPlainText("Erro Slucss", detalhe))
                // Android 13+ mostra a própria confirmação de cópia; repetir
                // com Toast duplicaria o aviso na tela.
                if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
                    Toast.makeText(this, "Erro copiado.", Toast.LENGTH_SHORT).show()
                }
            }
            .setNegativeButton("Fechar", null)
            .show()
    }

    private fun friendlyAnalysisError(error: Throwable): String {
        TransferErrors.message(error, "analysis")?.let { return it }
        val message = error.message.orEmpty()
        return when {
            error is LinkageError ->
                "Este aparelho não conseguiu iniciar o analisador. Atualize o aplicativo e tente novamente."
            LocalTools.isLocalEngineUnavailable(message) ->
                "Este aparelho (processador x86) não executa o analisador local. " +
                    "A busca pelo servidor deve funcionar; tente novamente."
            message.contains("Unsupported URL", true) ->
                "Este link ainda não é suportado. Confira se ele está completo."
            message.contains("private", true) || message.contains("login", true) ->
                "Este conteúdo exige login na plataforma de origem."
            message.contains("timed out", true) || message.contains("timeout", true) ->
                "A análise demorou demais. Verifique sua conexão e tente novamente."
            else ->
                "Não foi possível analisar este link. Verifique a conexão ou procure uma atualização."
        }
    }

    private fun formatDuration(seconds: Int): String =
        "%d:%02d".format(seconds / 60, seconds % 60)

    private fun retryJob(job: DownloadJob) {
        enqueue(job.copy(status = DownloadJob.STATUS_QUEUED, stage = "queued", progress = 0f,
            error = null, errorDetail = null, fallbackEligible = false,
            message = "Preparando nova tentativa…", updatedAt = System.currentTimeMillis()), binding.downloadMessage)
    }

    private fun shareJob(job: DownloadJob) {
        val uri = job.outputUri?.let(Uri::parse) ?: return
        try {
            startActivity(Intent.createChooser(Intent(Intent.ACTION_SEND).apply {
                type = job.outputMime ?: "application/octet-stream"
                putExtra(Intent.EXTRA_STREAM, uri)
                clipData = ClipData.newRawUri(job.outputName, uri)
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }, "Compartilhar arquivo"))
        } catch (_: Exception) { toast("Não foi possível compartilhar. Confira se o arquivo ainda existe.") }
    }

    private fun resetVideoPreview() {
        thumbnailGeneration++
        thumbnailJob?.cancel()
        binding.enqueueButton.isEnabled = false
        binding.videoPreview.visibility = View.GONE
        binding.downloadOptions.visibility = View.GONE
        binding.videoThumbnail.setImageDrawable(null)
        binding.videoInfoText.text = ""
        binding.analysisErrorButton.visibility = View.GONE
    }

    private fun loadThumbnail(url: String?) {
        thumbnailJob?.cancel()
        val generation = ++thumbnailGeneration
        val videoUrl = analyzedUrl
        binding.videoThumbnail.setImageDrawable(null)
        if (url.isNullOrBlank()) return
        thumbnailJob = lifecycleScope.launch {
            val bitmap = withContext(Dispatchers.IO) {
                runCatching {
                    val connection = URL(url).openConnection() as HttpURLConnection
                    try {
                        connection.connectTimeout = 8_000
                        connection.readTimeout = 8_000
                        val limit = 2 * 1024 * 1024
                        val bytes = connection.inputStream.use { input ->
                            val output = java.io.ByteArrayOutputStream()
                            val buffer = ByteArray(8192)
                            while (true) {
                                currentCoroutineContext().ensureActive()
                                val count = input.read(buffer)
                                if (count < 0) break
                                check(output.size() + count <= limit)
                                output.write(buffer, 0, count)
                            }
                            output.toByteArray()
                        }
                        val options = BitmapFactory.Options().apply { inJustDecodeBounds = true }
                        BitmapFactory.decodeByteArray(bytes, 0, bytes.size, options)
                        options.inSampleSize = 1
                        while (maxOf(options.outWidth, options.outHeight) / options.inSampleSize > 480) options.inSampleSize *= 2
                        options.inJustDecodeBounds = false
                        BitmapFactory.decodeByteArray(bytes, 0, bytes.size, options)
                    } finally { connection.disconnect() }
                }.getOrNull()
            }
            if (bitmap != null && generation == thumbnailGeneration && analyzedUrl == videoUrl &&
                binding.videoPreview.visibility == View.VISIBLE) binding.videoThumbnail.setImageBitmap(bitmap)
        }
    }

    private fun toast(message: String) = Toast.makeText(this, message, Toast.LENGTH_LONG).show()

    companion object {
        private const val TAG = "SlucssMain"
        private const val ANALYSIS_TIMEOUT_MS = 60_000L
    }
}

private class SimpleItemSelectedListener(
    private val selected: (Int) -> Unit,
) : android.widget.AdapterView.OnItemSelectedListener {
    override fun onItemSelected(
        parent: android.widget.AdapterView<*>?, view: View?, position: Int, id: Long,
    ) = selected(position)
    override fun onNothingSelected(parent: android.widget.AdapterView<*>?) = Unit
}
