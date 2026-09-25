"""
Configuração central do backend.

Tudo vem de variáveis de ambiente (.env). Nenhum segredo é hardcoded —
o .exe/.apk/site cliente jamais recebe estes valores.
"""

from __future__ import annotations

import math
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def effective_cpu_count() -> int:
    """Retorna a cota de CPU visível ao processo, incluindo cgroups/afinidade."""
    limits = [os.cpu_count() or 1]
    try:
        limits.append(len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        pass

    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        if quota != "max":
            limits.append(max(1, math.floor(int(quota) / int(period))))
    except (OSError, ValueError, ZeroDivisionError):
        try:
            quota = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
            period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
            if quota > 0:
                limits.append(max(1, math.floor(quota / period)))
        except (OSError, ValueError, ZeroDivisionError):
            pass
    return max(1, min(limits))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Banco / limites da API
    database_url: str = "postgresql+psycopg2://xard:xard@localhost:5432/xard"
    database_pool_size: int = Field(default=5, ge=1, le=50)
    database_max_overflow: int = Field(default=5, ge=0, le=100)
    api_limit_concurrency: int = Field(default=500, ge=10, le=5000)
    api_backlog: int = Field(default=256, ge=16, le=4096)
    api_graceful_shutdown_seconds: int = Field(default=30, ge=5, le=300)
    video_info_concurrency: int = Field(default=2, ge=1, le=16)
    video_info_cache_ttl_seconds: int = Field(default=900, ge=0, le=3600)
    video_info_cache_size: int = Field(default=128, ge=1, le=2048)

    # Fila persistente controlada pela própria API. O PostgreSQL é a fonte de
    # verdade; em memória ficam somente os processos que esta instância lidera.
    max_concurrent_tasks: int = Field(default=2, ge=1, le=32)
    task_timeout: int = Field(default=1800, ge=60, le=86_400)
    task_retention_time: int = Field(default=2_592_000, ge=3600, le=31_536_000)
    temp_file_retention: int = Field(default=86_400, ge=300, le=2_592_000)
    max_tasks_per_user: int = Field(default=2, ge=1, le=100)
    max_queue_size: int = Field(default=100, ge=1, le=10_000)
    task_poll_interval: float = Field(default=0.5, ge=0.1, le=10.0)
    task_stale_seconds: int = Field(default=120, ge=30, le=3600)
    task_max_retries: int = Field(default=3, ge=0, le=10)
    task_cancel_grace_seconds: int = Field(default=5, ge=1, le=60)
    task_cleanup_interval: int = Field(default=900, ge=60, le=86_400)
    task_orphan_cleanup_interval: int = Field(default=86_400, ge=300, le=604_800)
    task_storage_recovery_interval: int = Field(default=300, ge=30, le=86_400)
    task_storage_audit_interval: int = Field(default=900, ge=60, le=86_400)
    media_cache_recovery_interval: int = Field(default=60, ge=15, le=3600)
    progress_update_interval: float = Field(default=1.0, ge=0.25, le=30.0)
    progress_update_step: float = Field(default=2.0, ge=0.5, le=25.0)
    task_cancel_check_interval: float = Field(default=0.5, ge=0.1, le=5.0)

    # WebSocket / eventos. Cada instância sincroniza o estado persistido e
    # entrega somente às conexões autenticadas do dispositivo proprietário.
    websocket_enabled: bool = True
    websocket_ticket_ttl_seconds: int = 60
    websocket_heartbeat_seconds: int = 25
    websocket_pong_timeout_seconds: int = 60
    websocket_max_message_bytes: int = 16_384
    websocket_max_subscriptions: int = 100
    websocket_max_connections_per_device: int = 4
    websocket_max_connections_per_ip: int = 20
    websocket_messages_per_minute: int = 120
    websocket_client_queue_size: int = 100
    websocket_allowed_origins: str = ""

    # JWT (sessões temporárias anônimas)
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "xard-api"
    jwt_access_audience: str = "xard-api"
    jwt_websocket_audience: str = "xard-websocket"
    jwt_clock_skew_seconds: int = 15
    access_token_ttl_minutes: int = 10
    refresh_token_ttl_days: int = 7

    # Painel administrativo (vazio = admin desabilitado; fail closed)
    admin_api_key: str = ""

    # Chave de assinatura do servidor (Ed25519). Prioridade: PATH > B64 > PEM
    # literal > SERVER_KEYS_DIR (legado, arquivos no disco). Veja
    # backend/SERVER_SIGNING_KEY.md e backend/security/signing.py.
    server_signing_private_key_path: str = ""
    server_signing_private_key_b64: str = Field(default="", repr=False)
    server_signing_private_key_pem: str = Field(default="", repr=False)
    # Legado: diretório com server_ed25519_{private,public}.pem.
    server_keys_dir: str = "/srv/keys"

    # Assinatura de requisições / anti-replay
    signature_max_skew_seconds: int = 120
    challenge_ttl_seconds: int = 60

    # Versionamento do app
    app_latest_version: str = "1.6.0"
    app_minimum_version: str = "1.0.0"
    app_download_url: str = ""
    app_download_sha256: str = ""
    # Instalador servido em /app/installer (auto-update do desktop)
    app_installer_file: str = "./installer.exe"
    # O Upscale é autorizado pelo fluxo local assinado. A chave permite
    # interromper o recurso sem publicar outro cliente e a versão específica
    # evita bloquear funções antigas quando só o Upscale exigir atualização.
    upscale_enabled: bool = True
    upscale_minimum_version: str = "1.6.0"

    # Versão Android distribuída diretamente pelo site. Estes campos são
    # separados do desktop para que uma exigência de versão do Windows não
    # bloqueie um APK com numeração independente.
    android_latest_version: str = "1.2.1"
    android_latest_version_code: int = Field(default=21, ge=1)
    android_release_id: str = Field(default="", max_length=128)
    android_update_policy: Literal["optional", "recommended", "mandatory"] = "recommended"
    android_minimum_version: str = "1.0.0"
    android_minimum_version_code: int = Field(default=1, ge=1)
    # Universal: atende clientes antigos e arquiteturas não listadas abaixo.
    android_download_url: str = ""
    android_download_sha256: str = ""
    # Fatias por arquitetura. O APK universal carrega o FFmpeg/Python de todas
    # as ABIs (~45 MB cada); entregar só a fatia do aparelho corta quase metade
    # do download da atualização. Par vazio = arquitetura cai no universal.
    android_download_url_arm64: str = ""
    android_download_sha256_arm64: str = ""
    android_download_url_arm32: str = ""
    android_download_sha256_arm32: str = ""
    android_apk_file: str = "./Slucss-System.apk"
    # Bootstrap pequeno usado apenas na primeira instalação. Ele consulta o
    # manifesto assinado acima e baixa a fatia adequada para o aparelho.
    android_installer_url: str = ""
    android_installer_apk_file: str = "./Slucss-System-Installer-1.0.1.apk"
    android_changelog_json: str = "[]"
    android_processing_mode: Literal["local", "server", "auto"] = "local"
    android_server_fallback_enabled: bool = False
    # Compatibilidade do engine local Android. É separada do desktop porque os
    # ciclos de versão e os binários nativos são independentes.
    android_upscale_enabled: bool = True
    android_upscale_minimum_version: str = "1.2.0"
    android_upscale_minimum_version_code: int = Field(default=20, ge=1)
    android_upscale_engine_version: str = "ncnn-20260526-v1"
    android_animevideov3_model_version: str = "animevideov3-ncnn-v1"
    android_realcugan_model_version: str = "realcugan-se-conservative-v1"
    # APKs instalados pelo site não recebem PLAY_RECOGNIZED. Nesse modo a
    # defesa do Android usa a identidade Ed25519 por instalação, tokens curtos,
    # anti-replay, limites e bloqueios já aplicados a todo cliente da API.
    android_sideload_enabled: bool = True

    # Integridade do cliente
    allowed_windows_app_hashes: str = ""
    play_integrity_package_name: str = "com.xard.ytsystem"
    play_integrity_decode_url: str = "https://playintegrity.googleapis.com/v1"
    google_service_account_json: str = ""
    integrity_enforced: bool = True

    # Limites anti-abuso (sistema gratuito, identidade = dispositivo)
    daily_downloads_per_device: int = 30
    hourly_downloads_per_device: int = 10
    concurrent_downloads_per_device: int = 2
    max_file_size_mb: int = 2048
    max_gif_upload_mb: int = 80
    rate_limit_per_minute: int = 120
    sessions_per_ip_per_hour: int = 30

    # Worker / storage
    downloads_dir: str = "/srv/downloads"
    download_file_ttl_hours: int = 6
    storage_backend: Literal["local", "google_drive"] = "local"
    storage_allow_local_fallback: bool = True
    storage_keep_source_videos: bool = False
    storage_keep_downloaded_videos: bool = False
    storage_keep_generated_gifs: bool = True
    # Nomes novos e mais explícitos; None preserva a configuração legada.
    storage_keep_source_uploads: bool | None = None
    storage_keep_downloaded_source_videos: bool | None = None
    storage_local_fallback_max_size_mb: int = Field(default=10_240, ge=128, le=1_048_576)
    storage_recovery_ttl_hours: int = Field(default=24, ge=1, le=720)
    storage_local_fallback_ttl_seconds: int | None = Field(default=None, ge=300, le=2_592_000)
    download_token_ttl_seconds: int = Field(default=300, ge=30, le=3600)
    media_cache_enabled: bool = True
    media_cache_processing_stale_seconds: int = Field(default=3600, ge=120, le=86_400)
    media_cache_validation_timeout_seconds: int = Field(default=15, ge=3, le=60)
    temp_storage_max_mb: int = Field(default=4096, ge=256, le=1_048_576)
    temp_storage_min_free_mb: int = Field(default=256, ge=32, le=1_048_576)
    max_download_jobs: int = Field(default=1, ge=1, le=16)
    max_ffmpeg_jobs: int = Field(default=1, ge=1, le=16)
    task_admission_min_available_mb: int = Field(default=200, ge=0, le=8192)

    google_drive_auth_mode: Literal["service_account", "oauth"] = "oauth"
    google_drive_credentials_file: str = ""
    google_drive_credentials_base64: str = Field(default="", repr=False)
    google_drive_client_id: str = ""
    google_drive_client_secret: str = Field(default="", repr=False)
    google_drive_refresh_token: str = Field(default="", repr=False)
    google_drive_scope: Literal[
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/drive",
    ] = "https://www.googleapis.com/auth/drive.file"
    google_drive_shared_drive_id: str = ""
    google_drive_root_folder_id: str = ""
    google_drive_videos_folder_id: str = ""
    google_drive_gifs_folder_id: str = ""
    google_drive_upload_chunk_size_mb: int = Field(default=8, ge=1, le=64)
    google_drive_upload_timeout_seconds: int | None = Field(default=None, ge=30, le=3600)
    google_drive_upload_max_retries: int | None = Field(default=None, ge=0, le=10)
    remote_download_chunk_size_mb: int = Field(default=1, ge=1, le=16)
    google_drive_max_concurrent_uploads: int = Field(default=2, ge=1, le=16)
    google_drive_max_concurrent_downloads: int = Field(default=8, ge=1, le=64)
    google_drive_min_free_space_mb: int = Field(default=2048, ge=0, le=1_048_576)
    google_drive_max_file_size_mb: int = Field(default=2048, ge=1, le=1_048_576)
    google_drive_storage_alert_percent: int = Field(default=90, ge=1, le=100)
    google_drive_permanent_delete_expired: bool = True
    remote_cache_enabled: bool = False
    remote_cache_max_size_mb: int = Field(default=1024, ge=64, le=102_400)
    remote_cache_ttl_seconds: int = Field(default=1800, ge=60, le=86_400)
    remote_upload_timeout_seconds: int = Field(default=900, ge=30, le=3600)
    remote_download_timeout_seconds: int = Field(default=900, ge=30, le=3600)
    remote_upload_max_retries: int = Field(default=4, ge=0, le=10)
    remote_delete_max_retries: int = Field(default=4, ge=0, le=10)
    gif_ffmpeg_threads: int | None = Field(default=None, ge=1, le=32)
    gif_ffmpeg_filter_threads: int | None = Field(default=None, ge=1, le=32)
    gif_ffmpeg_timeout_seconds: int = Field(default=600, ge=30, le=3600)
    gif_max_encodings: int = Field(default=3, ge=1, le=4)
    gif_size_safety_margin: float = Field(default=0.88, ge=0.5, le=0.98)
    gif_min_fps: Literal[5, 10, 15] = 5
    gif_min_colors: Literal[32, 64, 128] = 32
    gif_min_width: int = Field(default=160, ge=160, le=640)
    gif_min_height: int = Field(default=120, ge=120, le=480)
    worker_memory_limit_mb: int | None = Field(default=None, ge=256, le=65_536)
    compatibility_transcode_min_memory_mb: int = Field(default=768, ge=256, le=8192)
    compatibility_transcode_min_available_mb: int = Field(default=384, ge=128, le=4096)
    nginx_internal_downloads_enabled: bool = False
    nginx_internal_downloads_uri: str = "/internal-downloads"
    # Cookies do YouTube (formato Netscape): contorna o bloqueio anti-bot
    # em IPs de datacenter. Vazio ou inexistente = segue sem cookies.
    ytdlp_cookies_file: str = ""
    # Cookies de sessão do X/Twitter (Netscape, base64), SEPARADOS do
    # ytdlp_cookies_file de propósito: aquele arquivo é servido inteiro pro
    # app em /video/cookies (COOKIES_SHARING_ENABLED) — se a sessão do X
    # estivesse nele, qualquer app autenticado conseguiria extrair o
    # auth_token da conta. Este valor só é lido pelo worker, nunca por uma
    # rota de API.
    twitter_cookies_b64: str = ""
    # Entrega os cookies de extração ao aplicativo Android, para ele resolver o
    # YouTube no próprio aparelho. É a MESMA sessão que o servidor usa: o
    # arquivo passa a existir em cada celular e pode ser extraído de lá. Se o
    # Google invalidar a sessão, o servidor perde o acesso junto — desligue
    # aqui e reexporte o cookies.txt. Padrão desligado, por isso.
    cookies_sharing_enabled: bool = False
    worker_binary_downloads_enabled: bool = True
    ffmpeg_windows_archive_sha256: str = "1438403bcc1104d9ecb47b04f8a818602b8d64839c374d607f231903b316eda1"
    ffmpeg_linux_archive_sha256: str = "7a19456683e31d937ae48d51e23dfb869dbb9db1e4d6e1b6881d7fed168fa5cf"
    deno_windows_archive_sha256: str = "5fb5bac71f609fb91ec8960fb290885aadc27eeb22f07a8eca0c3db6be38b11a"
    deno_linux_archive_sha256: str = "2d7bb6195226ac832e0bf7109a115f0af65ee69ac797a4bbde5b27a06cc242d9"
    # Flags do V8 aplicadas ao Deno que resolve os desafios JS do YouTube.
    # --max-semi-space-size=1 aperta só a geração jovem: mantém o JIT e segura
    # o pico do subprocesso em ~227 MB. O --lite-mode antigo economizava só
    # mais 10 MB e custava ~2 s por consulta, porque roda o solver sem JIT.
    # Vazio = comportamento padrão do V8 (~282 MB). Nunca usar
    # --max-old-space-size baixo: o solver estoura o limite e falha
    # SILENCIOSAMENTE (0 formatos) — medido com 128.
    deno_v8_flags: str = "--max-semi-space-size=1"

    # Notificacoes
    discord_download_webhook_url: str = ""
    discord_security_webhook_enabled: bool = False
    discord_security_webhook_url: str = ""
    discord_security_webhook_min_severity: str = "medium"
    discord_security_webhook_timeout_seconds: float = 5.0
    discord_security_webhook_include_full_ip: bool = False
    discord_security_webhook_rate_limit_per_minute: int = 20
    discord_security_webhook_environment: str = "production"
    security_log_hmac_key: str = ""

    # Headers de IP encaminhado só são aceitos de proxies autenticados ou
    # de endereços explicitamente confiáveis. Vazio = conexão TCP apenas.
    trusted_proxy_cidrs: str = ""
    internal_proxy_secret: str = ""

    # CORS (apenas o site oficial)
    web_origin: str = "https://xard.com"

    environment: str = "production"

    @field_validator(
        "storage_local_fallback_ttl_seconds",
        "google_drive_upload_timeout_seconds",
        "google_drive_upload_max_retries",
        "gif_ffmpeg_threads",
        "gif_ffmpeg_filter_threads",
        mode="before",
    )
    @classmethod
    def blank_env_means_auto(cls, value: object) -> object:
        """Env vazia = usar o padrão calculado (None), não erro de parsing."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("gif_min_fps", "gif_min_colors", mode="before")
    @classmethod
    def coerce_literal_int(cls, value: object) -> object:
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value)
        return value

    @field_validator("nginx_internal_downloads_uri")
    @classmethod
    def internal_download_uri_is_safe(cls, value: str) -> str:
        normalized = value.rstrip("/")
        if not normalized.startswith("/") or ".." in normalized or any(
            char in normalized for char in "\r\n"
        ):
            raise ValueError("NGINX_INTERNAL_DOWNLOADS_URI inválida")
        return normalized

    @field_validator("downloads_dir")
    @classmethod
    def downloads_dir_is_outside_source_tree(cls, value: str) -> str:
        """Impede que DOWNLOADS_DIR caia sobre o código-fonte do backend.

        No deploy plano (Square Cloud) o pacote `backend` é registrado apontando
        para a própria raiz da aplicação, então um caminho relativo como
        `./downloads` resolve para o mesmo diretório do pacote
        `backend/downloads/`. A varredura de `cleanup_orphaned_files` trataria
        router.py/schemas.py/service.py como resíduo sem registro no banco e os
        apagaria, derrubando a API com ModuleNotFoundError.
        """
        resolved = Path(value).resolve()
        source_root = Path(__file__).resolve().parent.parent
        collides = (
            resolved == source_root
            # Irmão dos pacotes do backend com nome importável (ex.: ./downloads).
            or (resolved.parent == source_root and resolved.name.isidentifier())
            or (resolved / "__init__.py").is_file()
        )
        if collides:
            raise ValueError(
                f"DOWNLOADS_DIR ({resolved}) colide com o código-fonte do backend; "
                "use um diretório dedicado, fora da árvore de pacotes Python"
            )
        return value

    @model_validator(mode="after")
    def storage_configuration_is_complete(self) -> "Settings":
        if self.google_drive_upload_timeout_seconds is not None:
            self.remote_upload_timeout_seconds = self.google_drive_upload_timeout_seconds
        if self.google_drive_upload_max_retries is not None:
            self.remote_upload_max_retries = self.google_drive_upload_max_retries
        if self.storage_backend != "google_drive":
            return self
        if not self.google_drive_root_folder_id:
            raise ValueError("A pasta raiz do Google Drive é obrigatória")
        if self.google_drive_auth_mode == "service_account":
            sources = int(bool(self.google_drive_credentials_file)) + int(
                bool(self.google_drive_credentials_base64)
            )
            if sources != 1:
                raise ValueError("Configure exatamente uma credencial de serviço do Google Drive")
        elif not all((
            self.google_drive_client_id,
            self.google_drive_client_secret,
            self.google_drive_refresh_token,
        )):
            raise ValueError("Credencial OAuth do Google Drive incompleta")
        return self

    @model_validator(mode="after")
    def production_must_fail_closed(self) -> "Settings":
        if self.environment.lower() != "production":
            return self
        errors: list[str] = []
        if self.jwt_algorithm != "HS256" or len(self.jwt_secret) < 32:
            errors.append("JWT_SECRET/algoritmo inválido")
        if "sslmode=require" not in self.database_url and "sslmode=verify-full" not in self.database_url:
            errors.append("DATABASE_URL deve exigir TLS")
        if not self.web_origin.startswith("https://"):
            errors.append("WEB_ORIGIN deve usar HTTPS")
        if self.app_download_url and not self.app_download_url.startswith("https://"):
            errors.append("APP_DOWNLOAD_URL deve usar HTTPS")
        if self.android_download_url and not self.android_download_url.startswith("https://"):
            errors.append("ANDROID_DOWNLOAD_URL deve usar HTTPS")
        if self.android_installer_url and not self.android_installer_url.startswith("https://"):
            errors.append("ANDROID_INSTALLER_URL deve usar HTTPS")
        if self.android_minimum_version_code > self.android_latest_version_code:
            errors.append("ANDROID_MINIMUM_VERSION_CODE não pode superar a versão mais recente")
        if self.android_download_url:
            android_hash = self.android_download_sha256.strip().lower()
            if len(android_hash) != 64 or any(
                char not in "0123456789abcdef" for char in android_hash
            ):
                errors.append("ANDROID_DOWNLOAD_SHA256 deve conter SHA-256 válido")
        android_slices = (
            (
                "ANDROID_DOWNLOAD_URL_ARM64",
                getattr(self, "android_download_url_arm64", "").strip(),
                getattr(self, "android_download_sha256_arm64", "").strip().lower(),
            ),
            (
                "ANDROID_DOWNLOAD_URL_ARM32",
                getattr(self, "android_download_url_arm32", "").strip(),
                getattr(self, "android_download_sha256_arm32", "").strip().lower(),
            ),
        )
        for name, url, sha256 in android_slices:
            if bool(url) != bool(sha256):
                errors.append(f"{name} e seu SHA-256 devem ser configurados juntos")
                continue
            if url and not url.startswith("https://"):
                errors.append(f"{name} deve usar HTTPS")
            if sha256 and (
                len(sha256) != 64
                or any(char not in "0123456789abcdef" for char in sha256)
            ):
                errors.append(f"{name.replace('_URL_', '_SHA256_')} deve conter SHA-256 válido")
        if self.discord_security_webhook_enabled and len(self.security_log_hmac_key) < 32:
            errors.append("SECURITY_LOG_HMAC_KEY é obrigatória para alertas")
        if len(self.internal_proxy_secret) < 32:
            errors.append("INTERNAL_PROXY_SECRET deve ter ao menos 32 caracteres")
        if self.worker_binary_downloads_enabled:
            hashes = {
                "FFMPEG_WINDOWS_ARCHIVE_SHA256": self.ffmpeg_windows_archive_sha256,
                "FFMPEG_LINUX_ARCHIVE_SHA256": self.ffmpeg_linux_archive_sha256,
                "DENO_WINDOWS_ARCHIVE_SHA256": self.deno_windows_archive_sha256,
                "DENO_LINUX_ARCHIVE_SHA256": self.deno_linux_archive_sha256,
            }
            for name, value in hashes.items():
                normalized = value.strip().lower()
                if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
                    errors.append(f"{name} deve conter SHA-256 válido")
        if errors:
            raise ValueError("Configuração de produção insegura: " + "; ".join(errors))
        return self

    @property
    def windows_hash_allowlist(self) -> set[str]:
        return {h.strip().lower() for h in self.allowed_windows_app_hashes.split(",") if h.strip()}

    @property
    def websocket_origin_allowlist(self) -> set[str]:
        origins = {self.web_origin.rstrip("/")}
        origins.update(
            origin.strip().rstrip("/")
            for origin in self.websocket_allowed_origins.split(",")
            if origin.strip()
        )
        return origins

    @property
    def resolved_task_concurrency(self) -> int:
        cpus = effective_cpu_count()
        return max(1, min(self.max_concurrent_tasks, cpus))

    @property
    def _safe_parallel_ffmpeg_threads(self) -> int:
        """Teto de threads por job de FFmpeg quando várias tarefas rodam juntas."""
        cpus = effective_cpu_count()
        return max(1, min(cpus // self.resolved_task_concurrency, 8))

    @property
    def resolved_gif_ffmpeg_threads(self) -> int:
        safe = self._safe_parallel_ffmpeg_threads
        return max(1, min(self.gif_ffmpeg_threads or safe, safe, 8))

    @property
    def resolved_gif_ffmpeg_filter_threads(self) -> int:
        safe = self.resolved_gif_ffmpeg_threads
        return max(1, min(self.gif_ffmpeg_filter_threads or safe, safe, 4))

    @property
    def resolved_compatibility_ffmpeg_threads(self) -> int:
        return self._safe_parallel_ffmpeg_threads


@lru_cache
def get_settings() -> Settings:
    # Public build metadata travels with the deployment; secrets remain in the
    # original .env. Reject unknown keys instead of accepting arbitrary settings.
    import json
    release_file = Path(__file__).resolve().parents[1] / "android-release.json"
    overrides = {}
    if release_file.is_file():
        overrides = json.loads(release_file.read_text(encoding="utf-8"))
        allowed = {"android_latest_version", "android_latest_version_code",
                   "android_release_id", "android_update_policy", "android_changelog_json",
                   "android_minimum_version_code", "android_download_url", "android_download_sha256",
                   "android_download_url_arm64", "android_download_sha256_arm64",
                   "android_download_url_arm32", "android_download_sha256_arm32"}
        if not isinstance(overrides, dict) or set(overrides) - allowed:
            raise ValueError("android-release.json contains unsupported configuration")
    return Settings(**overrides)
