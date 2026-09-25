package com.xard.ytsystem.bootstrap;

import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;

import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Locale;

import static org.junit.Assert.assertEquals;

public class ApkIntegrityTest {
    @Rule
    public final TemporaryFolder temporary = new TemporaryFolder();

    @Test
    public void acceptsOfficialPackageVersionAndCertificate() {
        ApkIntegrity.verifyMetadata(
                ApkIntegrity.EXPECTED_PACKAGE,
                12,
                List.of(ApkIntegrity.EXPECTED_CERTIFICATE_SHA256),
                12
        );
    }

    @Test(expected = SecurityException.class)
    public void rejectsAnotherPackage() {
        ApkIntegrity.verifyMetadata(
                "com.example.fake",
                12,
                List.of(ApkIntegrity.EXPECTED_CERTIFICATE_SHA256),
                12
        );
    }

    @Test(expected = SecurityException.class)
    public void rejectsAnotherVersion() {
        ApkIntegrity.verifyMetadata(
                ApkIntegrity.EXPECTED_PACKAGE,
                11,
                List.of(ApkIntegrity.EXPECTED_CERTIFICATE_SHA256),
                12
        );
    }

    @Test(expected = SecurityException.class)
    public void rejectsAnotherCertificate() {
        ApkIntegrity.verifyMetadata(
                ApkIntegrity.EXPECTED_PACKAGE,
                12,
                List.of("0".repeat(64)),
                12
        );
    }

    @Test(expected = SecurityException.class)
    public void rejectsMissingCertificate() {
        ApkIntegrity.verifyMetadata(
                ApkIntegrity.EXPECTED_PACKAGE,
                12,
                List.of(),
                12
        );
    }

    @Test(expected = SecurityException.class)
    public void rejectsMultipleCertificates() {
        ApkIntegrity.verifyMetadata(
                ApkIntegrity.EXPECTED_PACKAGE,
                12,
                List.of(
                        ApkIntegrity.EXPECTED_CERTIFICATE_SHA256,
                        ApkIntegrity.EXPECTED_CERTIFICATE_SHA256
                ),
                12
        );
    }

    @Test
    public void acceptsUppercaseCertificateHash() {
        ApkIntegrity.verifyMetadata(
                ApkIntegrity.EXPECTED_PACKAGE,
                12,
                List.of(ApkIntegrity.EXPECTED_CERTIFICATE_SHA256.toUpperCase(Locale.ROOT)),
                12
        );
    }

    @Test
    public void hashesFileWithSha256() throws Exception {
        File file = temporary.newFile("payload.apk");
        try (FileOutputStream output = new FileOutputStream(file)) {
            output.write("abc".getBytes(StandardCharsets.UTF_8));
        }
        assertEquals(
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
                ApkIntegrity.sha256(file)
        );
    }
}
