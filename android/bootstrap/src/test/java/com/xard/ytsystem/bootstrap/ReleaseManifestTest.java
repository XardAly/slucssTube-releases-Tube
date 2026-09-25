package com.xard.ytsystem.bootstrap;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

import org.junit.Test;

import static org.junit.Assert.assertEquals;

public class ReleaseManifestTest {
    private static final String ARM64_HASH = "a".repeat(64);
    private static final String ARM32_HASH = "b".repeat(64);
    private static final String UNIVERSAL_HASH = "c".repeat(64);
    private static final String SIGNED_MANIFEST = "{\"latest_version\":\"9.9.9\","
            + "\"latest_version_code\":99,\"minimum_version_code\":2,"
            + "\"download_url\":\"https://github.com/XardAly/slucssTube-releases-Tube/releases/download/android-v9.9.9/Slucss-System.apk\","
            + "\"sha256\":\"" + UNIVERSAL_HASH + "\",\"downloads\":{"
            + "\"arm64-v8a\":{\"url\":\"https://github.com/XardAly/slucssTube-releases-Tube/releases/download/android-v9.9.9/Slucss-System-arm64-v8a.apk\","
            + "\"sha256\":\"" + ARM64_HASH + "\"},"
            + "\"armeabi-v7a\":{\"url\":\"https://github.com/XardAly/slucssTube-releases-Tube/releases/download/android-v9.9.9/Slucss-System-armeabi-v7a.apk\","
            + "\"sha256\":\"" + ARM32_HASH + "\"}},\"changelog\":[\"Teste\"],"
            + "\"processing_mode\":\"local\",\"server_fallback_enabled\":true,"
            + "\"signature\":\"j1pCz5a3ntuMfSS+koPrSZauGMEAYCSIwvPCRSVKYn75r3IF1lwfXtORjYmwC0pyQqFpVCks7pT1eDQgUlhCCQ==\"}";

    private final JsonObject downloads = JsonParser.parseString(
            "{"
                    + "\"arm64-v8a\":{\"url\":\"https://github.com/XardAly/slucssTube-releases-Tube/releases/download/test/arm64.apk\",\"sha256\":\"" + ARM64_HASH + "\"},"
                    + "\"armeabi-v7a\":{\"url\":\"https://github.com/XardAly/slucssTube-releases-Tube/releases/download/test/arm32.apk\",\"sha256\":\"" + ARM32_HASH + "\"}"
                    + "}"
    ).getAsJsonObject();

    @Test
    public void completeBackendManifestIsVerifiedAndSelectsBestSlice() {
        ReleaseManifest manifest = ReleaseManifest.parseSigned(
                SIGNED_MANIFEST,
                new String[]{"arm64-v8a", "armeabi-v7a"},
                1
        );

        assertEquals(99, manifest.versionCode);
        assertEquals("9.9.9", manifest.versionName);
        assertEquals("arm64-v8a", manifest.download.abi);
    }

    @Test(expected = SecurityException.class)
    public void tamperedManifestIsRejectedBeforeItsFieldsAreTrusted() {
        ReleaseManifest.parseSigned(
                SIGNED_MANIFEST.replace("\"latest_version_code\":99", "\"latest_version_code\":98"),
                new String[]{"arm64-v8a"},
                1
        );
    }

    @Test
    public void arm64IsPreferredAccordingToDeviceOrder() {
        ReleaseManifest.Download selected = ReleaseManifest.chooseDownload(
                downloads,
                new String[]{"arm64-v8a", "armeabi-v7a"},
                "https://github.com/XardAly/slucssTube-releases-Tube/releases/download/test/universal.apk",
                UNIVERSAL_HASH
        );

        assertEquals("arm64-v8a", selected.abi);
        assertEquals(ARM64_HASH, selected.sha256);
    }

    @Test
    public void arm32DeviceReceivesItsSlice() {
        ReleaseManifest.Download selected = ReleaseManifest.chooseDownload(
                downloads,
                new String[]{"armeabi-v7a"},
                "https://example.com/universal.apk",
                UNIVERSAL_HASH
        );

        assertEquals("armeabi-v7a", selected.abi);
        assertEquals(
                "https://github.com/XardAly/slucssTube-releases-Tube/releases/download/test/arm32.apk",
                selected.url
        );
    }

    @Test
    public void missingArmSliceUsesSecureUniversalApk() {
        ReleaseManifest.Download selected = ReleaseManifest.chooseDownload(
                new JsonObject(),
                new String[]{"arm64-v8a"},
                "https://github.com/XardAly/slucssTube-releases-Tube/releases/download/test/universal.apk",
                UNIVERSAL_HASH
        );

        assertEquals("universal", selected.abi);
    }

    @Test(expected = SecurityException.class)
    public void x86OnlyDeviceIsRejectedBecauseFullAppHasNoCompatibleNativeLibraries() {
        ReleaseManifest.chooseDownload(
                downloads,
                new String[]{"x86_64", "x86"},
                "https://github.com/XardAly/slucssTube-releases-Tube/releases/download/test/universal.apk",
                UNIVERSAL_HASH
        );
    }

    @Test(expected = SecurityException.class)
    public void insecureUniversalUrlIsRejected() {
        ReleaseManifest.chooseDownload(
                new JsonObject(),
                new String[]{"arm64-v8a"},
                "http://example.com/universal.apk",
                UNIVERSAL_HASH
        );
    }

    @Test(expected = SecurityException.class)
    public void anotherHttpsHostIsRejected() {
        ReleaseManifest.chooseDownload(
                new JsonObject(),
                new String[]{"arm64-v8a"},
                "https://example.com/universal.apk",
                UNIVERSAL_HASH
        );
    }
}
