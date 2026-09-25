"""Regressão do consumo de memória e dos logs da consulta de vídeo."""

from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.downloads import router as downloads_router
from backend.downloads.schemas import VideoInfoRequest
from backend.observability import (
    _MaskingFormatter,
    _OperationalNoiseFilter,
    release_freed_memory,
    rss_mb,
    setup_logging,
)
from backend.workers import tasks

URL = "https://www.youtube.com/watch?v=abc123DEF45"
EQUIVALENT_URL = "https://youtu.be/abc123DEF45?si=compartilhado"
OTHER_URL = "https://www.youtube.com/watch?v=XYZ987abc12"


def test_filtro_remove_somente_ruido_interno_do_websocket():
    noise_filter = _OperationalNoiseFilter()
    websocket_record = logging.LogRecord(
        "uvicorn.error", logging.INFO, "", 0,
        '172.18.0.1:50168 - "WebSocket /websocket/downloads" [accepted]',
        (), None,
    )
    startup_record = logging.LogRecord(
        "uvicorn.error", logging.INFO, "", 0,
        "Uvicorn running on http://0.0.0.0:80", (), None,
    )
    error_record = logging.LogRecord(
        "uvicorn.error", logging.ERROR, "", 0,
        'Falha no WebSocket [accepted]', (), None,
    )

    assert not noise_filter.filter(websocket_record)
    assert noise_filter.filter(startup_record)
    assert noise_filter.filter(error_record)


@pytest.fixture(autouse=True)
def _limpa_estado_do_router(monkeypatch):
    downloads_router._video_info_cache.clear()
    downloads_router._video_info_inflight.clear()
    monkeypatch.setattr(
        downloads_router, "_video_info_slots", threading.BoundedSemaphore(1),
    )
    yield
    downloads_router._video_info_cache.clear()
    downloads_router._video_info_inflight.clear()


def _request_stub():
    return SimpleNamespace(state=SimpleNamespace(correlation_id="teste"))


def _chama_video_info(url=URL):
    return downloads_router.video_info(
        VideoInfoRequest(url=url), _request_stub(), ctx=None,
    )


def test_consultas_simultaneas_da_mesma_url_executam_yt_dlp_uma_vez(monkeypatch):
    chamadas = []
    liberado = threading.Event()

    def run(url):
        chamadas.append(url)
        liberado.wait(timeout=5)
        return {"title": "video", "video_qualities": []}

    monkeypatch.setattr(downloads_router, "fetch_info", run)

    resultados: list[dict] = []
    threads = [
        threading.Thread(target=lambda: resultados.append(_chama_video_info()))
        for _ in range(3)
    ]
    for thread in threads:
        thread.start()
    time.sleep(0.2)
    liberado.set()
    for thread in threads:
        thread.join(timeout=5)

    assert len(chamadas) == 1
    assert len(resultados) == 3
    assert all(item["title"] == "video" for item in resultados)
    assert downloads_router._video_info_inflight == {}


def test_falha_do_lider_propaga_para_quem_aguardava(monkeypatch):
    liberado = threading.Event()

    def run(url):
        liberado.wait(timeout=5)
        raise RuntimeError("extração falhou")

    monkeypatch.setattr(downloads_router, "fetch_info", run)

    erros: list[int] = []

    def chama():
        try:
            _chama_video_info()
        except HTTPException as exc:
            erros.append(exc.status_code)

    threads = [threading.Thread(target=chama) for _ in range(2)]
    for thread in threads:
        thread.start()
    time.sleep(0.2)
    liberado.set()
    for thread in threads:
        thread.join(timeout=5)

    assert erros == [422, 422]
    assert downloads_router._video_info_inflight == {}


def test_cache_evita_segunda_extracao(monkeypatch):
    chamadas = []
    monkeypatch.setattr(
        downloads_router, "fetch_info",
        lambda url: chamadas.append(url) or {"title": "v"},
    )
    _chama_video_info()
    _chama_video_info()
    assert len(chamadas) == 1


def test_urls_equivalentes_compartilham_extracao_em_andamento(monkeypatch):
    chamadas = []
    iniciou = threading.Event()
    liberado = threading.Event()

    def run(url):
        chamadas.append(url)
        iniciou.set()
        liberado.wait(timeout=5)
        return {"id": "abc123DEF45", "title": "video"}

    monkeypatch.setattr(downloads_router, "fetch_info", run)
    resultados = []
    threads = [
        threading.Thread(target=lambda: resultados.append(_chama_video_info(URL))),
        threading.Thread(
            target=lambda: resultados.append(_chama_video_info(EQUIVALENT_URL)),
        ),
    ]
    threads[0].start()
    assert iniciou.wait(timeout=2)
    threads[1].start()
    time.sleep(0.05)
    liberado.set()
    for thread in threads:
        thread.join(timeout=5)

    assert len(chamadas) == 1
    assert len(resultados) == 2
    assert downloads_router._video_info_inflight == {}


def test_cache_reaproveita_url_equivalente(monkeypatch):
    chamadas = []
    monkeypatch.setattr(
        downloads_router, "fetch_info",
        lambda url: chamadas.append(url) or {"id": "abc123DEF45", "title": "v"},
    )

    _chama_video_info(URL)
    _chama_video_info(EQUIVALENT_URL)

    assert len(chamadas) == 1


def test_cache_cria_alias_para_id_extraido_de_link_curto(monkeypatch):
    short_url = "https://vm.tiktok.com/ZMabcdef/"
    direct_url = "https://www.tiktok.com/@autor/video/7512345678901234567"
    chamadas = []
    monkeypatch.setattr(
        downloads_router, "fetch_info",
        lambda url: chamadas.append(url) or {
            "id": "7512345678901234567", "title": "video",
        },
    )

    _chama_video_info(short_url)
    _chama_video_info(direct_url)

    assert len(chamadas) == 1
    assert len(downloads_router._video_info_cache) == 2


def test_alias_respeita_limite_lru_e_preserva_link_consultado(monkeypatch):
    short_url = "https://vm.tiktok.com/ZMabcdef/"
    info = {"id": "7512345678901234567", "title": "video"}
    monkeypatch.setattr(downloads_router.settings, "video_info_cache_size", 1)

    downloads_router._store_video_info(short_url, info)

    assert len(downloads_router._video_info_cache) == 1
    assert downloads_router._cached_video_info(short_url) is info


def test_espera_vaga_curta_sem_aumentar_concorrencia(monkeypatch):
    iniciou_primeiro = threading.Event()
    libera_primeiro = threading.Event()
    lock = threading.Lock()
    ativos = 0
    pico = 0

    def run(url):
        nonlocal ativos, pico
        with lock:
            ativos += 1
            pico = max(pico, ativos)
        try:
            if "abc123DEF45" in url:
                iniciou_primeiro.set()
                libera_primeiro.wait(timeout=5)
            return {"id": url[-11:], "title": "video"}
        finally:
            with lock:
                ativos -= 1

    monkeypatch.setattr(downloads_router, "fetch_info", run)
    monkeypatch.setattr(downloads_router, "_VIDEO_INFO_SLOT_WAIT_SECONDS", 0.5)
    resultados = []
    threads = [
        threading.Thread(target=lambda: resultados.append(_chama_video_info(URL))),
        threading.Thread(target=lambda: resultados.append(_chama_video_info(OTHER_URL))),
    ]
    threads[0].start()
    assert iniciou_primeiro.wait(timeout=2)
    threads[1].start()
    time.sleep(0.05)
    libera_primeiro.set()
    for thread in threads:
        thread.join(timeout=5)

    assert len(resultados) == 2
    assert pico == 1
    assert downloads_router._video_info_inflight == {}


def test_timeout_da_vaga_mantem_503_e_limpa_inflight(monkeypatch):
    slots = downloads_router._video_info_slots
    assert slots.acquire(blocking=False)
    chamadas = []
    monkeypatch.setattr(
        downloads_router, "fetch_info", lambda url: chamadas.append(url) or {},
    )
    monkeypatch.setattr(downloads_router, "_VIDEO_INFO_SLOT_WAIT_SECONDS", 0.02)
    try:
        with pytest.raises(HTTPException) as erro:
            _chama_video_info(OTHER_URL)
    finally:
        slots.release()

    assert erro.value.status_code == 503
    assert erro.value.headers == {"Retry-After": "2"}
    assert chamadas == []
    assert downloads_router._video_info_inflight == {}


def test_configure_youtube_runtime_limita_memoria_do_deno(monkeypatch):
    monkeypatch.delenv("DENO_V8_FLAGS", raising=False)
    monkeypatch.setattr(tasks, "ensure_deno", lambda: "/caminho/deno")
    monkeypatch.setattr(tasks.settings, "deno_v8_flags", "--lite-mode")

    opts: dict = {}
    tasks._configure_youtube_runtime(opts, URL)

    assert opts["js_runtimes"]["deno"]["path"] == "/caminho/deno"
    import os
    assert os.environ["DENO_V8_FLAGS"] == "--lite-mode"


def test_configure_youtube_runtime_respeita_flag_da_hospedagem(monkeypatch):
    monkeypatch.setenv("DENO_V8_FLAGS", "--custom-da-host")
    monkeypatch.setattr(tasks, "ensure_deno", lambda: "/caminho/deno")
    monkeypatch.setattr(tasks.settings, "deno_v8_flags", "--lite-mode")

    tasks._configure_youtube_runtime({}, URL)

    import os
    assert os.environ["DENO_V8_FLAGS"] == "--custom-da-host"


def test_fetch_info_repete_sem_cookie_quando_so_recebe_storyboard(monkeypatch):
    import yt_dlp

    tentativas = []

    class FakeYdl:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, url, download):
            tentativas.append((url, download, self.opts.get("cookiefile")))
            if self.opts.get("cookiefile"):
                return {
                    "id": "abc123DEF45",
                    "title": "somente imagens",
                    "formats": [{
                        "format_id": "sb0", "vcodec": "none", "acodec": "none",
                    }],
                }
            return {
                "id": "abc123DEF45",
                "title": "video",
                "formats": [
                    {
                        "format_id": "137", "height": 1080, "fps": 30,
                        "vcodec": "avc1.640028", "acodec": "none", "tbr": 4500,
                    },
                    {
                        "format_id": "140", "vcodec": "none",
                        "acodec": "mp4a.40.2", "abr": 128,
                    },
                ],
            }

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYdl)
    monkeypatch.setattr(tasks, "_resolve_cookiefile", lambda: "cookies.txt")
    monkeypatch.setattr(tasks, "_configure_youtube_runtime", lambda *_args: None)

    info = tasks.fetch_info(URL)

    assert [q["height"] for q in info["video_qualities"]] == [1080]
    assert [q["abr"] for q in info["audio_qualities"]] == [128]
    assert [item[2] for item in tentativas] == ["cookies.txt", None]


def test_fallback_de_download_repete_erro_de_cookie_uma_vez(monkeypatch):
    import yt_dlp

    tentativas = []

    class FakeYdl:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, url, download):
            tentativas.append((url, download, self.opts.get("cookiefile")))
            if self.opts.get("cookiefile"):
                raise yt_dlp.utils.DownloadError(
                    "ERROR: [youtube] teste: The page needs to be reloaded."
                )
            return {"id": "abc123DEF45"}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYdl)

    result = tasks._extract_with_cookie_fallback(
        yt_dlp,
        URL,
        {"cookiefile": "cookies.txt", "quiet": True},
        download=True,
        result_builder=lambda _ydl, info: info["id"],
    )

    assert result == "abc123DEF45"
    assert [item[2] for item in tentativas] == ["cookies.txt", None]


def test_fetch_info_nao_aceita_storyboard_como_video(monkeypatch):
    import yt_dlp

    class FakeYdl:
        def __init__(self, _opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download):
            assert download is False
            return {
                "id": "abc123DEF45",
                "title": "somente imagens",
                "formats": [{
                    "format_id": "sb0", "vcodec": "none", "acodec": "none",
                }],
            }

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYdl)
    monkeypatch.setattr(tasks, "_resolve_cookiefile", lambda: None)
    monkeypatch.setattr(tasks, "_configure_youtube_runtime", lambda *_args: None)

    with pytest.raises(RuntimeError, match="qualidades"):
        tasks.fetch_info(URL)


def test_formatter_mascara_segredos():
    formatter = _MaskingFormatter("%(message)s")
    record = logging.LogRecord(
        "x", logging.INFO, __file__, 1,
        "entrega token=abc123 signature=xyz Authorization: Bearer eyJhbGc.abc url?X-Goog-Signature=zz",
        None, None,
    )
    rendered = formatter.format(record)
    assert "abc123" not in rendered
    assert "xyz" not in rendered
    assert "eyJhbGc" not in rendered
    assert "zz" not in rendered
    assert "token=***" in rendered


def test_rss_mb_retorna_valor_positivo_com_psutil():
    assert rss_mb() > 0


def test_release_freed_memory_nunca_levanta_excecao():
    # No Windows é no-op; no Linux chama malloc_trim. Em ambos, jamais falha.
    release_freed_memory()
    release_freed_memory()


def test_setup_logging_e_idempotente():
    setup_logging("teste-unitario")
    handlers = list(logging.getLogger().handlers)
    assert logging.getLogger("uvicorn.access").level == logging.WARNING
    setup_logging("teste-unitario")
    assert logging.getLogger().handlers == handlers


# --------------------------------------------------------------------------
# Link do TikTok vindo do botão "compartilhar"
# --------------------------------------------------------------------------

TIKTOK_COMPARTILHADO = (
    "https://www.tiktok.com/@vmpr6ss/video/7665751062028012807?_r=1"
    "&u_code=dgjm14mfbgjagf&preview_pb=0&sharer_language=pt&_d=f4246f8m2km43m"
    "&share_item_id=7665751062028012807&source=h5_m&timestamp=1784910470"
    "&sec_user_id=MS4wLjABAAAAV2gJmPLs5TeKB45dy6XVKLXhGK7h9pB1fHEMsY"
    "&utm_source=copy&utm_campaign=client_share&utm_medium=android"
    "&share_app_id=1233&enable_checksum=1&sp_level=1"
)
TIKTOK_CANONICO = "https://www.tiktok.com/@vmpr6ss/video/7665751062028012807"


def test_link_compartilhado_do_tiktok_perde_o_rastreio():
    assert tasks._canonical_tiktok_url(TIKTOK_COMPARTILHADO) == TIKTOK_CANONICO


@pytest.mark.parametrize("caminho", [
    "/@usuario/video/7512345678901234567",
    "/@usuario.com/photo/7512345678901234567",
    "/video/7512345678901234567",
    "/embed/7512345678901234567",
    "/v/7512345678901234567/",
])
def test_canoniza_todos_os_formatos_de_post(caminho):
    sujo = f"https://www.tiktok.com{caminho}?_r=1&share_iid=99#topo"
    assert tasks._canonical_tiktok_url(sujo) == f"https://www.tiktok.com{caminho}"


@pytest.mark.parametrize("url", [
    # Sem rastreio: devolvido intacto (nem reescreve, nem realoca).
    TIKTOK_CANONICO,
    # O parâmetro é a própria identificação do vídeo nessas plataformas.
    "https://www.youtube.com/watch?v=abc123DEF45&t=30",
    "https://www.instagram.com/reel/Cabc123/?igsh=xyz",
    "https://br.pinterest.com/pin/12345/?nic_v3=1",
    "https://x.com/usuario/status/1234567890?s=20",
    # Encurtador do TikTok: só o redirecionamento revela o vídeo.
    "https://vm.tiktok.com/ZMabcdef/?_r=1",
    # Perfil e busca não são posts.
    "https://www.tiktok.com/@usuario?lang=pt",
    "https://www.tiktok.com/tag/gato?enable_checksum=1",
])
def test_nao_mexe_em_link_que_nao_e_post_do_tiktok(url):
    assert tasks._canonical_tiktok_url(url) == url


def test_resolve_expande_o_encurtador_e_depois_limpa(monkeypatch):
    # O redirecionamento do vm.tiktok.com entrega justamente o link cheio de
    # rastreio: a limpeza precisa acontecer DEPOIS da expansão.
    monkeypatch.setattr(
        tasks, "_expand_tiktok_short_url", lambda _url: TIKTOK_COMPARTILHADO,
    )
    assert tasks._resolve_platform_url("https://vm.tiktok.com/ZMabcdef/") == TIKTOK_CANONICO
