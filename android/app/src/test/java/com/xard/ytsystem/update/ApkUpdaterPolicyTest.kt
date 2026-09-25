package com.xard.ytsystem.update

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ApkUpdaterPolicyTest {
    @Test
    fun aceitaSomenteAssetHttpsDoRepositorioOficial() {
        assertTrue(
            ApkUpdater.isOfficialDownloadUrl(
                "https://github.com/XardAly/slucssTube-releases-Tube/" +
                    "releases/download/android-v1.1.2/Slucss-System-arm64-v8a.apk",
            ),
        )
        assertFalse(
            ApkUpdater.isOfficialDownloadUrl(
                "http://github.com/XardAly/slucssTube-releases-Tube/" +
                    "releases/download/android-v1.1.2/Slucss-System.apk",
            ),
        )
        assertFalse(
            ApkUpdater.isOfficialDownloadUrl(
                "https://example.com/XardAly/slucssTube-releases-Tube/" +
                    "releases/download/android-v1.1.2/Slucss-System.apk",
            ),
        )
        assertFalse(
            ApkUpdater.isOfficialDownloadUrl(
                "https://github.com/outro/repositorio/releases/download/v1/app.apk",
            ),
        )
    }
}
