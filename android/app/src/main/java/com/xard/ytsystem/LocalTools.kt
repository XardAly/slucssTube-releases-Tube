package com.xard.ytsystem

import android.content.Context
import android.util.Log
import com.xard.ytsystem.api.ApiClient
import com.yausername.ffmpeg.FFmpeg
import com.yausername.youtubedl_android.YoutubeDL
import java.io.File
import java.net.URI
import java.util.concurrent.TimeUnit
import java.util.concurrent.locks.ReentrantReadWriteLock

object LocalTools {
    private const val TAG = "SlucssTools"
    // Mesmo layout que a youtubedl-android usa ao descompactar os pacotes.
    private const val PACKAGES_DIR = "youtubedl-android/packages"

    @Volatile private var youtubeDlReady = false
    @Volatile private var ffmpegReady = false

    @Synchronized
    fun ensureYoutubeDlInitialized(context: Context) {
        if (youtubeDlReady) return
        YoutubeDL.getInstance().init(context.applicationContext)
        val target = File(context.noBackupFilesDir, "youtubedl-android/yt-dlp/yt-dlp")
        val expected = "1fa6733c37ea6fb51c99ad8fe785e7b7e5f3246c9b980230329d4fb72ed8d4d6"
        fun hash(file: File): String = file.inputStream().use { input ->
            val digest = java.security.MessageDigest.getInstance("SHA-256")
            val buffer = ByteArray(65536)
            while (true) { val n = input.read(buffer); if (n < 0) break; digest.update(buffer, 0, n) }
            digest.digest().joinToString("") { "%02x".format(it) }
        }
        if (!target.isFile || hash(target) != expected) {
            val partial = File(target.parentFile, "yt-dlp.part")
            try {
                context.resources.openRawResource(R.raw.ytdlp).use { input ->
                    partial.outputStream().use { output -> input.copyTo(output) }
                }
                check(hash(partial) == expected) { "O extrator embarcado não passou na verificação de integridade." }
                java.nio.file.Files.move(partial.toPath(), target.toPath(),
                    java.nio.file.StandardCopyOption.REPLACE_EXISTING, java.nio.file.StandardCopyOption.ATOMIC_MOVE)
            } finally { partial.delete() }
        }
        youtubeDlReady = true
    }

    @Synchronized
    fun ensureFfmpegInitialized(context: Context) {
        if (ffmpegReady) return
        val app = context.applicationContext
        val preferences = app.getSharedPreferences("bundled_tools", Context.MODE_PRIVATE)
        if (preferences.getInt("ffmpeg_bundle_revision", 0) < 23) {
            // Upstream's version stays 0.18.1. Remove only its extracted cache
            // once so an update also installs the corrected WebP libraries.
            val extracted = File(app.noBackupFilesDir, "$PACKAGES_DIR/ffmpeg")
            check(!extracted.exists() || extracted.deleteRecursively()) { "Não foi possível renovar as ferramentas de mídia." }
        }
        FFmpeg.getInstance().init(app)
        check(File(ffmpegExecutable(app)).canExecute()) { "FFmpeg não foi incluído para esta arquitetura." }
        preferences.edit().putInt("ffmpeg_bundle_revision", 23).commit()
        ffmpegReady = true
    }

    fun ensureInitialized(context: Context) {
        ensureYoutubeDlInitialized(context)
        ensureFfmpegInitialized(context)
    }

    /**
     * Erros que denunciam o yt-dlp embarcado defasado, e não um problema do
     * link. O APK sai de fábrica com a versão que a youtubedl-android empacota
     * como recurso raw — meses atrás da estável —, então a plataforma reescreve
     * o extrator e só a atualização resolve.
     *
     * As quatro primeiras aparecem no download: a extração passa e a mídia é
     * recusada. As duas últimas são falhas de extração; "webpage video data" em
     * especial **só existe nas versões antigas** (o extrator do TikTok foi
     * reescrito depois), então ela é um sinal exato de defasagem.
     *
     * Só entram aqui mensagens conferidas no código do yt-dlp. Erro que o
     * usuário pode resolver — link sem suporte, vídeo removido, IP bloqueado,
     * verificação de robô — fica de fora: atualizar não muda nada.
     */
    fun isStaleExtractorFailure(detail: String): Boolean = STALE_EXTRACTOR_SIGNS.any {
        detail.contains(it, ignoreCase = true)
    }

    private val STALE_EXTRACTOR_SIGNS = listOf(
        "HTTP Error 403",
        "unable to download video data",
        "nsig extraction failed",
        "Signature extraction failed",
        "Unable to extract webpage video data",
        "Failed to extract any player response",
    )

    /**
     * O aparelho não consegue EXECUTAR o motor local (Python/yt-dlp/FFmpeg)
     * como processo. Acontece em Android x86/x86_64 — emuladores de jogo e
     * PCs — com o APK de release, que embarca o motor só para ARM: o sistema
     * instala o app pelo tradutor ARM→x86 e a interface roda, mas o execve do
     * subprocesso devolve ENOEXEC, a libc repassa o binário ao shell e o mksh
     * tenta lê-lo como texto — daí "libpython.so[6]: no closing quote".
     *
     * Atualizar o yt-dlp não muda nada e repetir só falha de novo: o caminho
     * para esses aparelhos é o download pelo servidor.
     */
    fun isLocalEngineUnavailable(detail: String): Boolean = ENGINE_UNAVAILABLE_SIGNS.any {
        detail.contains(it, ignoreCase = true)
    }

    private val ENGINE_UNAVAILABLE_SIGNS = listOf(
        "no closing quote",           // mksh lendo o ELF ARM como script
        "Exec format error",          // ENOEXEC reportado direto
        "cannot execute binary file",
    )

    /**
     * A atualização reescreve o arquivo do yt-dlp que os processos em execução
     * estão lendo. `updateYoutubeDL` é `synchronized`, mas a execução não é:
     * sem esta trava, trocar o extrator no meio de um download entrega bytes
     * pela metade ao interpretador. Busca e download rodam em paralelo (a tela
     * corre extração local contra a API), então os dois caminhos podem pedir a
     * atualização ao mesmo tempo.
     *
     * Leitura = executar o yt-dlp (vários ao mesmo tempo, como antes).
     * Escrita = atualizar (espera todos terminarem e barra novas execuções).
     */
    private val ytdlpGate = ReentrantReadWriteLock()

    /** Roda o yt-dlp garantindo que nenhuma atualização troque o arquivo no meio. */
    fun <T> withYoutubeDl(block: () -> T): T {
        ytdlpGate.readLock().lock()
        try {
            return block()
        } finally {
            ytdlpGate.readLock().unlock()
        }
    }

    /** Engines are updated only by a verified, user-approved APK update. */
    @Suppress("UNUSED_PARAMETER")
    fun refreshYoutubeDl(context: Context): Boolean {
        Log.i(TAG, "extractor_update_requires_signed_apk")
        return false
    }

    fun ffmpegExecutable(context: Context): String =
        "${context.applicationInfo.nativeLibraryDir}/libffmpeg.so"

    /**
     * O libffmpeg.so é só o executável — libavdevice, libavcodec e as demais
     * ficam no pacote descompactado pelo FFmpeg.init(). Sem LD_LIBRARY_PATH o
     * linker recusa o binário com "CANNOT LINK EXECUTABLE". A youtubedl-android
     * monta esse ambiente para o yt-dlp; quem chama o FFmpeg direto (GIF e
     * conversão) precisa montá-lo por conta própria.
     */
    fun ffmpegLibraryPath(context: Context): String {
        val packages = File(context.applicationContext.noBackupFilesDir, PACKAGES_DIR)
        return listOf("ffmpeg/usr/lib", "python/usr/lib")
            .joinToString(":") { File(packages, it).absolutePath }
    }

    /**
     * O QuickJS já vem empacotado no APK, mas o yt-dlp não o encontra sozinho:
     * sem `--js-runtimes` ele avisa que não há runtime JavaScript e extrai o
     * YouTube em modo degradado. Este valor é o que deve ser passado na opção.
     */
    fun jsRuntimeOption(context: Context): String =
        "quickjs:${context.applicationInfo.nativeLibraryDir}/libqjs.so"

    /**
     * Mesmo par de cabeçalhos que o backend usa em `_ytdlp_base_opts`. Sem eles
     * o TikTok trata a requisição como automatizada e cobra impersonation, que
     * não existe no Python embarcado (não há curl_cffi no aparelho).
     */
    const val BROWSER_USER_AGENT: String =
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

    const val ACCEPT_LANGUAGE_HEADER: String =
        "Accept-Language:pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"

    const val TIKTOK_REFERER: String = "https://www.tiktok.com/"

    /**
     * Mesmos clientes que o servidor usa em `_ytdlp_base_opts`.
     *
     * Sem isto o yt-dlp inclui os clientes web, que exigem PO Token e levam à
     * verificação de robô — foi o que passou a acontecer quando o runtime JS
     * entrou na 1.0.4.
     */
    const val YOUTUBE_EXTRACTOR_ARGS: String =
        "youtube:player_client=default,-android_sdkless,android_vr"

    /** Caminhos de um post do TikTok: /@usuario/video/ID, /video/ID, /photo/ID... */
    private val TIKTOK_POST_PATH =
        Regex("""^/(?:@[\w.\-]+/(?:video|photo)|video|photo|embed|v)/\d+/?$""")

    fun isTikTokLink(url: String): Boolean {
        val host = runCatching { URI(url).host?.lowercase() }.getOrNull() ?: return false
        return isTikTokHost(host)
    }

    private fun isTikTokHost(host: String): Boolean =
        host == "tiktok.com" || host.endsWith(".tiktok.com")

    /**
     * Link limpo para análise e download, igual ao que o servidor faz em
     * `_canonical_tiktok_url`.
     *
     * O botão "compartilhar" do TikTok gera links com dezenas de parâmetros de
     * rastreio (share_iid, sec_user_id, enable_checksum, timestamp...) emitidos
     * para o aparelho e o IP de quem compartilhou. Repetidos a partir daqui, o
     * TikTok recusa a extração ("Your IP address is blocked from accessing this
     * post") — enquanto o mesmo vídeo baixa normalmente pelo link canônico.
     */
    fun canonicalLink(url: String): String {
        val parsed = runCatching { URI(url) }.getOrNull() ?: return url
        if (parsed.rawQuery == null && parsed.rawFragment == null) return url
        val host = parsed.host?.lowercase() ?: return url
        if (!isTikTokHost(host)) return url
        val path = parsed.rawPath ?: return url
        if (!TIKTOK_POST_PATH.matches(path)) return url
        return "${parsed.scheme}://${parsed.host}$path"
    }

    private const val COOKIES_FILE = "extraction-cookies.txt"
    private const val COOKIES_TTL_MS = 6L * 60 * 60 * 1000

    /**
     * Caminho do arquivo de cookies, renovado pelo servidor no máximo a cada 6h.
     *
     * Fica no armazenamento privado do aplicativo. Retorna null quando o
     * servidor não compartilha cookies — e aí a extração segue sem eles.
     */
    suspend fun cookiesFile(context: Context, api: ApiClient): String? {
        val appContext = context.applicationContext
        val arquivo = File(appContext.noBackupFilesDir, COOKIES_FILE)
        val idade = System.currentTimeMillis() - arquivo.lastModified()
        if (arquivo.isFile && arquivo.length() > 0 && idade in 0 until COOKIES_TTL_MS) {
            return arquivo.absolutePath
        }
        return runCatching {
            val conteudo = api.youtubeCookies()
            require(conteudo.isNotBlank()) { "cookies vazios" }
            arquivo.writeText(conteudo)
            Log.i(TAG, "cookies_atualizados linhas=${conteudo.lines().size}")
            arquivo.absolutePath
        }.getOrElse { error ->
            Log.w(TAG, "cookies_indisponiveis reason=${error.javaClass.simpleName}")
            // Um arquivo antigo ainda ajuda mais do que nenhum.
            arquivo.takeIf { it.isFile && it.length() > 0 }?.absolutePath
        }
    }
}
