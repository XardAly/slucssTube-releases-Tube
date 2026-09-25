package com.xard.ytsystem.api

import com.google.gson.JsonParser
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class ArquiteturaDoApkTest {
    private val hashArm64 = "a".repeat(64)
    private val hashArm32 = "b".repeat(64)

    private fun downloads(json: String) = JsonParser.parseString(json).asJsonObject

    private val publicado = downloads(
        """
        {
          "arm64-v8a":   {"url": "https://exemplo/arm64.apk",   "sha256": "$hashArm64"},
          "armeabi-v7a": {"url": "https://exemplo/armeabi.apk", "sha256": "$hashArm32"}
        }
        """.trimIndent(),
    )

    @Test
    fun escolheAFatiaNaOrdemDePreferenciaDoAparelho() {
        val fatia = fatiaDaArquitetura(publicado, listOf("arm64-v8a", "armeabi-v7a"))
        assertEquals("https://exemplo/arm64.apk" to hashArm64, fatia)
    }

    @Test
    fun aparelho32BitsRecebeAFatiaDele() {
        val fatia = fatiaDaArquitetura(publicado, listOf("armeabi-v7a"))
        assertEquals("https://exemplo/armeabi.apk" to hashArm32, fatia)
    }

    @Test
    fun arquiteturaSemFatiaCaiNoUniversal() {
        assertNull(fatiaDaArquitetura(publicado, listOf("x86_64", "x86")))
        assertNull(fatiaDaArquitetura(null, listOf("arm64-v8a")))
        assertNull(fatiaDaArquitetura(publicado, emptyList()))
    }

    @Test
    fun entradaComHashInvalidoNaoEUsada() {
        // Baixar 50 MB e só então reprovar na verificação é pior do que ignorar.
        val quebrado = downloads(
            """
            {
              "arm64-v8a": {"url": "https://exemplo/arm64.apk", "sha256": "nao-e-hash"},
              "armeabi-v7a": {"url": "", "sha256": "$hashArm32"}
            }
            """.trimIndent(),
        )
        assertNull(fatiaDaArquitetura(quebrado, listOf("arm64-v8a", "armeabi-v7a")))
    }
}
