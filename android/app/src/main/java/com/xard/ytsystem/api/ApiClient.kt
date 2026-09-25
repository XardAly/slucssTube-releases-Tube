package com.xard.ytsystem.api

import android.content.Context
import android.os.Build
import android.os.SystemClock
import android.util.Log
import com.google.gson.Gson
import com.google.gson.GsonBuilder
import com.google.gson.JsonElement
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import com.xard.ytsystem.BuildConfig
import com.xard.ytsystem.security.DeviceIdentity
import com.xard.ytsystem.security.SecureStore
import com.xard.ytsystem.security.ServerSignature
import com.xard.ytsystem.upscale.UpscaleModel
import com.xard.ytsystem.upscale.UpscaleOptions
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.security.MessageDigest
import java.io.File
import java.io.FileOutputStream
import java.util.UUID
import java.util.concurrent.TimeUnit

class ApiClient(context: Context) {
    private val appContext = context.applicationContext
    private val identity = DeviceIdentity(appContext)
    private val secureStore = SecureStore(appContext)
    private val gson: Gson = GsonBuilder().serializeNulls().disableHtmlEscaping().create()
    private val http = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .callTimeout(40, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .followSslRedirects(false)
        .build()
    // Large files may take minutes; keep the idle read timeout, without a 40-second total deadline.
    private val fileHttp = http.newBuilder().callTimeout(0, TimeUnit.SECONDS).build()
    private val eventHttp = http.newBuilder().callTimeout(5, TimeUnit.SECONDS).retryOnConnectionFailure(false).build()
    private val sessionMutex = Mutex()
    @Volatile private var session: Session? = loadSession()

    suspend fun authorizeDownload(
        url: String,
        mode: String,
        height: Int?,
        videoFormat: String,
        audioFormat: String,
    ): LocalPermit {
        val body = JsonObject().apply {
            addProperty("operation", "download")
            addProperty("url", url)
            addProperty("mode", mode)
            if (height == null) add("height", null) else addProperty("height", height)
            add("format_id", null)
            addProperty("video_format", videoFormat)
            addProperty("audio_format", audioFormat)
            add("gif_options", null)
        }
        val expected = JsonObject().apply {
            addProperty("mode", mode)
            if (height == null) add("height", null) else addProperty("height", height)
            add("format_id", null)
            addProperty("video_format", videoFormat)
            addProperty("audio_format", audioFormat)
        }
        return authorizeLocal(body, "download", url, expected)
    }

    suspend fun authorizeGif(options: GifOptions): LocalPermit {
        val apiOptions = gson.toJsonTree(options).asJsonObject.renameGifFields()
        val body = JsonObject().apply {
            addProperty("operation", "gif")
            add("url", null)
            add("mode", null)
            add("height", null)
            add("format_id", null)
            addProperty("video_format", "mp4")
            addProperty("audio_format", "mp3")
            add("gif_options", apiOptions)
        }
        return authorizeLocal(body, "gif", "", apiOptions)
    }

    suspend fun authorizeCompatibility(): LocalPermit {
        val body = JsonObject().apply {
            addProperty("operation", "compatibility")
            add("url", null)
            add("mode", null)
            add("height", null)
            add("format_id", null)
            addProperty("video_format", "mp4")
            addProperty("audio_format", "mp3")
            add("gif_options", null)
        }
        return authorizeLocal(body, "compatibility", "", JsonObject())
    }

    suspend fun authorizeUpscale(options: UpscaleOptions): LocalPermit {
        val model = UpscaleModel.fromApi(options.model)
        val upscale = JsonObject().apply {
            addProperty("media_kind", "video")
            addProperty("model", model.apiValue)
            addProperty("scale", options.scale)
            addProperty("output_format", options.outputFormat)
            addProperty("model_version", options.modelVersion)
            addProperty("engine_version", options.engineVersion)
        }
        val body = JsonObject().apply {
            addProperty("operation", "upscale")
            add("upscale_options", upscale)
        }
        return authorizeLocal(body, "upscale", "", upscale)
    }

    private suspend fun authorizeLocal(
        requestBody: JsonObject,
        expectedOperation: String,
        expectedUrl: String,
        expectedParameters: JsonObject,
    ): LocalPermit {
        val totalStarted = SystemClock.elapsedRealtime()
        var phase = "challenge"
        var challengeMillis = -1L
        try {
            val challengeStarted = SystemClock.elapsedRealtime()
            val challenge = signedJson("POST", "/security/challenge", null)
                .requireString("challenge")
            challengeMillis = SystemClock.elapsedRealtime() - challengeStarted
            requestBody.addProperty("challenge", challenge)
            requestBody.addProperty(
                "challenge_signature",
                identity.sign(challenge.toByteArray(Charsets.UTF_8)),
            )
            phase = "authorize"
            val authorizeStarted = SystemClock.elapsedRealtime()
            val response = signedJson("POST", "/local/authorize", requestBody)
            val authorizeMillis = SystemClock.elapsedRealtime() - authorizeStarted
            val permit = response.getAsJsonObject("permit")
                ?: throw ApiException(500, "A API retornou uma autorização inválida.")
            val signature = response.requireString("signature")
            if (!ServerSignature.verify(permit, signature)) {
                throw ApiException(500, "A autorização recebida não é autêntica.")
            }
            val now = System.currentTimeMillis() / 1000
            val operationId = permit.requireString("operation_id")
            val expiresAt = permit.get("expires_at")?.asLong ?: 0
            val valid = permit.requireString("device_id") == identity.deviceId &&
                permit.requireString("operation") == expectedOperation &&
                permit.requireString("url") == expectedUrl &&
                permit.getAsJsonObject("parameters") == expectedParameters &&
                expiresAt > now && expiresAt - now <= 16 * 60
            if (!valid) throw ApiException(500, "A autorização recebida não corresponde ao pedido.")
            Log.i(
                TAG,
                "local_authorize_timing challenge_ms=$challengeMillis " +
                    "authorize_ms=$authorizeMillis total_ms=${SystemClock.elapsedRealtime() - totalStarted}",
            )
            return LocalPermit(operationId, expiresAt)
        } catch (error: Exception) {
            Log.w(
                TAG,
                "local_authorize_failed phase=$phase challenge_ms=$challengeMillis " +
                    "total_ms=${SystemClock.elapsedRealtime() - totalStarted} " +
                    "type=${error.javaClass.simpleName}",
            )
            throw error
        }
    }

    suspend fun reportDownloadEvent(event: JsonObject) {
        signedJson("POST", "/app/android/download-events", event, retry = false, requestClient = eventHttp)
    }

    suspend fun reportLocalProgress(
        operationId: String,
        status: String,
        stage: String,
        progress: Float,
        message: String,
    ) {
        val body = JsonObject().apply {
            addProperty("status", status)
            addProperty("stage", stage)
            addProperty("progress", progress.coerceIn(0f, 100f))
            addProperty("message", message.take(160))
        }
        signedJson("POST", "/local/$operationId/progress", body, retry = false, requestClient = eventHttp)
    }

    suspend fun createServerDownload(
        url: String,
        mode: String,
        height: Int?,
        videoFormat: String,
        audioFormat: String,
    ): String {
        val challenge = signedJson("POST", "/security/challenge", null)
            .requireString("challenge")
        val body = JsonObject().apply {
            addProperty("url", url)
            addProperty("mode", mode)
            if (height == null) add("height", null) else addProperty("height", height)
            add("format_id", null)
            addProperty("video_format", videoFormat)
            addProperty("audio_format", audioFormat)
            addProperty("challenge", challenge)
            addProperty("challenge_signature", identity.sign(challenge.toByteArray(Charsets.UTF_8)))
        }
        return signedJson("POST", "/download/create", body).requireString("id")
    }

    /**
     * Cookies de extração servidos pelo servidor.
     *
     * Chamada assinada como as demais: só um dispositivo registrado recebe. O
     * conteúdo nunca é registrado em log.
     */
    suspend fun youtubeCookies(): String =
        signedJson("GET", "/video/cookies", null).requireString("cookies")

    /**
     * Metadados pela API, para quando o YouTube recusa a extração no aparelho.
     *
     * O servidor usa cookies e não cai na verificação de robô que atinge redes
     * comuns; sem esta saída o aplicativo simplesmente não acha o vídeo.
     */
    suspend fun videoInfo(url: String): RemoteVideoInfo {
        val body = JsonObject().apply { addProperty("url", url) }
        val response = signedJson("POST", "/video/info", body)
        val alturas = response.getAsJsonArray("video_qualities")
            ?.mapNotNull { it.asJsonObject.get("height")?.takeIf { h -> !h.isJsonNull }?.asInt }
            ?.filter { it > 0 }
            ?.distinct()
            ?.sortedDescending()
            .orEmpty()
        return RemoteVideoInfo(
            title = response.get("title")?.takeIf { !it.isJsonNull }?.asString.orEmpty()
                .ifBlank { "Vídeo" },
            uploader = response.get("uploader")?.takeIf { !it.isJsonNull }?.asString,
            duration = response.get("duration")?.takeIf { !it.isJsonNull }?.asInt ?: 0,
            heights = alturas,
            thumbnail = response.get("thumbnail")?.takeIf { !it.isJsonNull }?.asString,
        )
    }

    suspend fun serverDownloadStatus(id: String): ServerDownloadStatus {
        val response = signedJson("GET", "/download/status/$id", null)
        return ServerDownloadStatus(
            id = id,
            status = response.requireString("status"),
            progress = response.get("progress")?.takeIf { !it.isJsonNull }?.asFloat ?: 0f,
            stage = response.get("stage")?.takeIf { !it.isJsonNull }?.asString ?: "processing",
            message = response.get("progress_message")?.takeIf { !it.isJsonNull }?.asString
                ?: "Processando no servidor...",
            fileUrl = response.get("file_url")?.takeIf { !it.isJsonNull }?.asString,
            fileName = response.get("file_name")?.takeIf { !it.isJsonNull }?.asString,
            error = response.get("error")?.takeIf { !it.isJsonNull }?.asString,
        )
    }

    suspend fun cancelServerTask(id: String) {
        signedJson("POST", "/tasks/$id/cancel", null)
    }

    suspend fun downloadServerFile(
        path: String,
        destination: File,
        shouldCancel: () -> Boolean,
        onProgress: (Long, Long) -> Unit,
    ) = withContext(Dispatchers.IO) {
        val url = if (path.startsWith("https://")) path else BuildConfig.API_BASE_URL + path
        val request = Request.Builder().url(url).get().build()
        try {
            fileHttp.newCall(request).execute().use { response ->
                if (!response.isSuccessful) throw ApiException(response.code, "Falha ao receber o arquivo pronto.")
                val body = response.body ?: throw ApiException(500, "O servidor retornou um arquivo vazio.")
                val expected = body.contentLength()
                var total = 0L
                body.byteStream().use { input ->
                    FileOutputStream(destination).use { output ->
                        val buffer = ByteArray(256 * 1024)
                        while (true) {
                            if (shouldCancel()) throw ApiException(499, "Cancelado.")
                            val read = input.read(buffer)
                            if (read < 0) break
                            total += read
                            output.write(buffer, 0, read)
                            onProgress(total, expected)
                        }
                        output.fd.sync()
                    }
                }
                if (expected >= 0 && total != expected) {
                    destination.delete()
                    throw ApiException(500, "O arquivo recebido ficou incompleto.")
                }
            }
        } catch (error: Exception) {
            destination.delete()
            throw error
        }
    }

    suspend fun androidManifest(): AndroidManifest = withContext(Dispatchers.IO) {
        val response = executeJson(
            Request.Builder()
                .url("${BuildConfig.API_BASE_URL}/app/android/version")
                .header("User-Agent", "Slucss-Android/${BuildConfig.VERSION_NAME}")
                .get()
                .build(),
        )
        val signature = response.requireString("signature")
        response.remove("signature")
        if (!ServerSignature.verify(response, signature)) {
            throw ApiException(500, "A verificação de segurança da atualização falhou.")
        }
        // Prefere a fatia da arquitetura deste aparelho: o APK universal traz o
        // FFmpeg/Python de todas as ABIs e quase dobra o tamanho da atualização.
        val fatia = fatiaDaArquitetura(
            response.getAsJsonObject("downloads"),
            Build.SUPPORTED_ABIS?.toList().orEmpty(),
        )
        AndroidManifest(
            latestVersion = response.requireString("latest_version"),
            latestVersionCode = response.get("latest_version_code")?.asInt ?: 0,
            minimumVersionCode = response.get("minimum_version_code")?.asInt ?: 0,
            downloadUrl = (fatia?.first ?: response.requireString("download_url")).ifBlank {
                "${BuildConfig.API_BASE_URL}/app/android/apk"
            },
            sha256 = fatia?.second ?: response.requireString("sha256").lowercase(),
            changelog = response.getAsJsonArray("changelog")?.mapNotNull {
                it.takeIf(JsonElement::isJsonPrimitive)?.asString
            } ?: emptyList(),
            processingMode = response.requireString("processing_mode"),
            serverFallbackEnabled = response.get("server_fallback_enabled")?.asBoolean == true,
            upscaleEnabled = response.getAsJsonObject("upscale")
                ?.get("enabled")?.asBoolean == true,
            upscaleMinimumVersionCode = response.getAsJsonObject("upscale")
                ?.get("minimum_version_code")?.asInt ?: Int.MAX_VALUE,
            upscaleEngineVersion = response.getAsJsonObject("upscale")
                ?.get("engine_version")?.asString.orEmpty(),
            animeVideoV3ModelVersion = response.getAsJsonObject("upscale")
                ?.getAsJsonObject("models")?.get("realesr_animevideov3")?.asString.orEmpty(),
            realCuganModelVersion = response.getAsJsonObject("upscale")
                ?.getAsJsonObject("models")?.get("realcugan")?.asString.orEmpty(),
            releaseId = response.get("release_id")?.asString
                ?: "android-${response.get("latest_version_code")?.asInt ?: 0}",
            publishedAt = response.get("published_at")?.asString.orEmpty(),
            updatePolicy = response.get("update_policy")?.asString ?: "recommended",
        )
    }

    private suspend fun signedJson(
        method: String,
        path: String,
        body: JsonObject?,
        retry: Boolean = true,
        requestClient: OkHttpClient = http,
    ): JsonObject = withContext(Dispatchers.IO) {
        // Telemetry must never acquire the session mutex or initiate registration/refresh.
        val active = if (requestClient === eventHttp) {
            session?.takeIf { it.expiresAt > System.currentTimeMillis() + 5_000 }
                ?: return@withContext JsonObject()
        } else ensureSession()
        val payload = body?.let(gson::toJson)?.toByteArray(Charsets.UTF_8) ?: ByteArray(0)
        val timestamp = (System.currentTimeMillis() / 1000).toString()
        val requestId = UUID.randomUUID().toString()
        val bodyHash = sha256(payload)
        val canonical = "$method\n${path.substringBefore('?')}\n$timestamp\n$requestId\n$bodyHash"
        val builder = Request.Builder()
            .url(BuildConfig.API_BASE_URL + path)
            .header("Authorization", "Bearer ${active.accessToken}")
            .header("Content-Type", JSON.toString())
            .header("X-Device-ID", identity.deviceId)
            .header("X-App-Version", BuildConfig.VERSION_NAME.substringBefore('-'))
            .header("X-Timestamp", timestamp)
            .header("X-Request-ID", requestId)
            .header("X-Signature", identity.sign(canonical.toByteArray(Charsets.UTF_8)))
        val request = when (method) {
            "POST" -> builder.post(payload.toRequestBody(JSON)).build()
            "GET" -> builder.get().build()
            else -> error("Método não suportado")
        }
        try {
            executeJson(request, requestClient)
        } catch (error: ApiException) {
            if (error.statusCode == 401 && retry) {
                clearSession()
                signedJson(method, path, body, retry = false, requestClient = requestClient)
            } else {
                throw error
            }
        }
    }

    private suspend fun ensureSession(): Session = sessionMutex.withLock {
        session?.takeIf { it.expiresAt > System.currentTimeMillis() + 30_000 }?.let { return it }
        session?.refreshToken?.let { refresh ->
            try {
                return storeSession(
                    publicPost("/session/refresh", JsonObject().apply {
                        addProperty("refresh_token", refresh)
                    }),
                )
            } catch (_: ApiException) {
                clearSession()
            }
        }
        val device = JsonObject().apply {
            addProperty("device_id", identity.deviceId)
            addProperty("installation_id", identity.installationId)
            add("hardware_hash", null)
            addProperty("public_key", identity.publicKeyPem)
            addProperty("platform", "android")
            addProperty("app_version", BuildConfig.VERSION_NAME.substringBefore('-'))
            add("app_hash", null)
            add("play_integrity_token", null)
        }
        storeSession(publicPost("/session/create", JsonObject().apply { add("device", device) }))
    }

    private fun publicPost(path: String, body: JsonObject): JsonObject {
        val request = Request.Builder()
            .url(BuildConfig.API_BASE_URL + path)
            .post(gson.toJson(body).toRequestBody(JSON))
            .build()
        return executeJson(request)
    }

    private fun executeJson(request: Request, client: OkHttpClient = http): JsonObject {
        try {
            client.newCall(request).execute().use { response ->
                val bytes = response.body?.source()?.run {
                    request(MAX_RESPONSE_BYTES + 1L)
                    readByteArray(minOf(buffer.size, MAX_RESPONSE_BYTES + 1L))
                } ?: ByteArray(0)
                if (bytes.size > MAX_RESPONSE_BYTES) {
                    throw ApiException(response.code, "A resposta da API é grande demais.")
                }
                val json = runCatching {
                    JsonParser.parseString(bytes.toString(Charsets.UTF_8)).asJsonObject
                }.getOrElse { JsonObject() }
                if (!response.isSuccessful) {
                    val detail = json.get("detail")?.takeIf { it.isJsonPrimitive }?.asString
                    throw ApiException(response.code, detail ?: friendlyHttpError(response.code))
                }
                return json
            }
        } catch (error: ApiException) {
            throw error
        } catch (_: Exception) {
            throw ApiException(0, "Sem conexão com a API. Verifique sua internet.")
        }
    }

    private fun storeSession(json: JsonObject): Session {
        val expiresIn = json.get("expires_in")?.asLong ?: 0
        val stored = Session(
            accessToken = json.requireString("access_token"),
            refreshToken = json.requireString("refresh_token"),
            expiresAt = System.currentTimeMillis() + expiresIn * 1000,
        )
        session = stored
        secureStore.put(SESSION_KEY, gson.toJson(stored).toByteArray(Charsets.UTF_8))
        return stored
    }

    private fun loadSession(): Session? = secureStore.get(SESSION_KEY)?.let {
        runCatching { gson.fromJson(it.toString(Charsets.UTF_8), Session::class.java) }.getOrNull()
    }

    private fun clearSession() {
        session = null
        secureStore.put(SESSION_KEY, null)
    }

    private fun JsonObject.requireString(name: String): String =
        get(name)?.takeIf { it.isJsonPrimitive }?.asString
            ?: throw ApiException(500, "Resposta inválida da API.")

    private fun JsonObject.renameGifFields(): JsonObject = JsonObject().also { output ->
        entrySet().forEach { (key, value) ->
            val apiKey = when (key) {
                "customWidth" -> "custom_width"
                "customHeight" -> "custom_height"
                "loopCount" -> "loop_count"
                "maxSizeMb" -> "max_size_mb"
                else -> key
            }
            output.add(apiKey, value)
        }
    }

    private fun sha256(value: ByteArray): String = MessageDigest.getInstance("SHA-256")
        .digest(value).joinToString("") { "%02x".format(it) }

    private fun friendlyHttpError(code: Int): String = when (code) {
        401 -> "Sua sessão expirou. Tente novamente."
        403 -> "Este aparelho não foi autorizado."
        426 -> "Atualize o Slucss System para continuar."
        429 -> "Muitas tentativas. Aguarde um pouco."
        else -> "A API está indisponível no momento."
    }

    private data class Session(
        val accessToken: String,
        val refreshToken: String,
        val expiresAt: Long,
    )

    companion object {
        private const val TAG = "SlucssApi"
        private val JSON = "application/json; charset=utf-8".toMediaType()
        private const val MAX_RESPONSE_BYTES = 1024 * 1024
        private const val SESSION_KEY = "api_session"
    }
}

/**
 * Par (url, sha256) publicado para a arquitetura do aparelho, se houver.
 *
 * `abis` chega na ordem de preferência do próprio Android. Entradas com hash
 * fora do formato são ignoradas de propósito: cair no APK universal é melhor do
 * que baixar dezenas de MB e só depois falhar na verificação de integridade.
 */
internal fun fatiaDaArquitetura(
    downloads: JsonObject?,
    abis: List<String>,
): Pair<String, String>? {
    if (downloads == null) return null
    for (abi in abis) {
        val entrada = downloads.getAsJsonObject(abi) ?: continue
        val url = entrada.get("url")?.asString.orEmpty()
        val sha = entrada.get("sha256")?.asString.orEmpty().lowercase()
        if (url.isNotBlank() && sha.matches(Regex("[0-9a-f]{64}"))) return url to sha
    }
    return null
}
