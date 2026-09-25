package com.xard.ytsystem

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class LocalToolsTest {
    private val compartilhado =
        "https://www.tiktok.com/@vmpr6ss/video/7665751062028012807?_r=1" +
            "&u_code=dgjm14mfbgjagf&preview_pb=0&sharer_language=pt" +
            "&share_item_id=7665751062028012807&source=h5_m&timestamp=1784910470" +
            "&sec_user_id=MS4wLjABAAAAV2gJmPLs5TeKB45dy6XVKLXhGK7h9pB1fHEMsY" +
            "&utm_source=copy&utm_campaign=client_share&utm_medium=android" +
            "&share_app_id=1233&enable_checksum=1&sp_level=1"

    private val canonico = "https://www.tiktok.com/@vmpr6ss/video/7665751062028012807"

    @Test
    fun linkCompartilhadoDoTiktokPerdeORastreio() {
        // Os parâmetros do botão "compartilhar" são emitidos para o aparelho e o
        // IP de quem compartilhou; repetidos daqui o TikTok responde
        // "Your IP address is blocked from accessing this post".
        assertEquals(canonico, LocalTools.canonicalLink(compartilhado))
    }

    @Test
    fun canonizaTodosOsFormatosDePost() {
        for (caminho in listOf(
            "/@usuario/video/7512345678901234567",
            "/@usuario.com/photo/7512345678901234567",
            "/video/7512345678901234567",
            "/embed/7512345678901234567",
            "/v/7512345678901234567/",
        )) {
            val sujo = "https://www.tiktok.com$caminho?_r=1&share_iid=99#topo"
            assertEquals("https://www.tiktok.com$caminho", LocalTools.canonicalLink(sujo))
        }
    }

    @Test
    fun naoMexeEmLinkQueNaoEPostDoTiktok() {
        for (url in listOf(
            // Sem rastreio: devolvido intacto.
            canonico,
            // O parâmetro é a própria identificação do vídeo nessas plataformas.
            "https://www.youtube.com/watch?v=abc123DEF45&t=30",
            "https://www.instagram.com/reel/Cabc123/?igsh=xyz",
            "https://br.pinterest.com/pin/12345/?nic_v3=1",
            // Encurtador: só o redirecionamento revela o vídeo.
            "https://vm.tiktok.com/ZMabcdef/?_r=1",
            // Perfil e busca não são posts.
            "https://www.tiktok.com/@usuario?lang=pt",
            "https://www.tiktok.com/tag/gato?enable_checksum=1",
        )) {
            assertEquals(url, LocalTools.canonicalLink(url))
        }
    }

    @Test
    fun linkIlegivelSegueIntactoEmVezDeQuebrarAAnalise() {
        val quebrado = "https://www.tiktok.com/@user/video/123?x=|invalido|"
        assertEquals(quebrado, LocalTools.canonicalLink(quebrado))
    }

    @Test
    fun reconheceSomenteHostsReaisDoTiktok() {
        assertTrue(LocalTools.isTikTokLink("https://vt.tiktok.com/ZSVpML8wd/"))
        assertTrue(LocalTools.isTikTokLink(canonico))
        assertFalse(LocalTools.isTikTokLink("https://tiktok.com.exemplo.test/video/123"))
        assertFalse(LocalTools.isTikTokLink("https://www.youtube.com/watch?v=abc"))
        assertFalse(LocalTools.isTikTokLink("link invalido"))
    }

    @Test
    fun falhaDeExtratorDefasadoPedeAtualizacao() {
        for (detalhe in listOf(
            // Erro real registrado em produção: o APK sai com um yt-dlp meses
            // atrás da estável e o extrator do TikTok já foi reescrito desde
            // então. A mensagem só existe nas versões antigas.
            "ERROR: [TikTok] 7611331590874074398: Unable to extract webpage " +
                "video data; please report this issue on https://github.com/yt-dlp/",
            "ERROR: [youtube] abc: Failed to extract any player response",
            "ERROR: unable to download video data: HTTP Error 403: Forbidden",
            "WARNING: [youtube] abc: nsig extraction failed",
            "ERROR: [youtube] abc: Signature extraction failed",
        )) {
            assertTrue(detalhe, LocalTools.isStaleExtractorFailure(detalhe))
        }
    }

    @Test
    fun erroDoUsuarioNaoDisparaAtualizacao() {
        for (detalhe in listOf(
            "ERROR: Unsupported URL: https://exemplo.com/video",
            "ERROR: [youtube] abc: Video unavailable",
            "ERROR: [TikTok] 123: Your IP address is blocked from accessing this post",
            "ERROR: [youtube] abc: Sign in to confirm you're not a bot",
            "ERROR: [TikTok] 123: Unable to extract universal data for rehydration",
            "ERROR: unable to write data: No space left on device",
            // Motor inexecutável (aparelho x86): atualizar o yt-dlp não muda
            // nada — não pode disparar o refresh.
            ERRO_X86,
            "",
        )) {
            assertFalse(detalhe, LocalTools.isStaleExtractorFailure(detalhe))
        }
    }

    @Test
    fun motorInexecutavelEmAparelhoX86EReconhecido() {
        // Erro real copiado da tela de um emulador Android 9 x86_64: o APK de
        // release só traz o motor ARM, o execve devolve ENOEXEC e o shell lê o
        // ELF como texto.
        for (detalhe in listOf(
            ERRO_X86,
            "sh: /path/libpython.so: Exec format error",
            "cannot execute binary file",
        )) {
            assertTrue(detalhe, LocalTools.isLocalEngineUnavailable(detalhe))
        }
    }

    @Test
    fun errosComunsNaoViramDiagnosticoDeAparelhoX86() {
        for (detalhe in listOf(
            "ERROR: [youtube] abc: Video unavailable",
            "ERROR: unable to download video data: HTTP Error 403: Forbidden",
            "ERROR: Requested format is not available",
            "",
        )) {
            assertFalse(detalhe, LocalTools.isLocalEngineUnavailable(detalhe))
        }
    }

    private companion object {
        const val ERRO_X86 =
            "YoutubeDLException: /data/app/com.xard.ytsystem-1JhqUZWRvOBd==/lib/arm64/" +
                "libpython.so[6]: no closing quote"
    }
}
