package com.xard.ytsystem

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class SlucssApplicationTest {
    @Test
    fun limpezaFicaRestritaAosArtefatosDoAtualizador() {
        assertTrue(SlucssApplication.shouldDeleteUpdateArtifact("Slucss-System.apk"))
        assertTrue(SlucssApplication.shouldDeleteUpdateArtifact("Slucss-System.apk.part"))
        assertTrue(
            SlucssApplication.shouldDeleteUpdateArtifact(
                "Slucss-System-123e4567-e89b-12d3-a456-426614174000.apk.part",
            ),
        )
        assertFalse(SlucssApplication.shouldDeleteUpdateArtifact("video.mp4"))
        assertFalse(SlucssApplication.shouldDeleteUpdateArtifact("outro.apk"))
        assertFalse(SlucssApplication.shouldDeleteUpdateArtifact("Slucss-System.apk.bak"))
    }
}
