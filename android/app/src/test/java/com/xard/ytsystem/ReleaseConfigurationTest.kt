package com.xard.ytsystem

import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class ReleaseConfigurationTest {
    @Test
    fun commonsCompressUsadoPeloYoutubeDlNaoPodeSerOfuscado() {
        val rules = sequenceOf(
            File("app/proguard-rules.pro"),
            File("proguard-rules.pro"),
        ).firstOrNull(File::isFile)?.readText()
            ?: error("proguard-rules.pro não encontrado")

        assertTrue(
            "A release volta a fechar em YoutubeDL.initPython sem esta regra",
            rules.contains("-keep class org.apache.commons.compress.** { *; }"),
        )
    }
}
