package com.xard.ytsystem.download

import org.junit.Assert.assertEquals
import org.junit.Test

class ProgressParserTest {
    @Test
    fun extraiBytesVelocidadeEtaSemGuardarOLinhaInteira() {
        val result = ProgressParser.parse(
            "[download]  65.0% of 185.00MiB at 12.00MiB/s ETA 00:06",
            65f,
            6,
        )
        assertEquals(65f, result.percent)
        assertEquals(185L * 1024 * 1024, result.totalBytes)
        assertEquals(12L * 1024 * 1024, result.speedBytes)
        assertEquals(6, result.etaSeconds)
    }

    @Test
    fun linhaMalformadaMantemValoresSeguros() {
        val result = ProgressParser.parse("x".repeat(20_000), -1f, -1)
        assertEquals(0f, result.percent)
        assertEquals(0, result.downloadedBytes)
        assertEquals(0, result.totalBytes)
    }
}
