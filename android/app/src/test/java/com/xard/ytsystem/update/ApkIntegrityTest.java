package com.xard.ytsystem.update;

import org.junit.Test;

import java.util.List;
import java.util.Locale;

public class ApkIntegrityTest {
    @Test
    public void acceptsOfficialPackageVersionAndCertificate() {
        ApkIntegrity.verifyMetadata(
                ApkIntegrity.EXPECTED_PACKAGE,
                13,
                List.of(ApkIntegrity.EXPECTED_CERTIFICATE_SHA256),
                13
        );
    }

    @Test(expected = SecurityException.class)
    public void rejectsAnotherPackage() {
        ApkIntegrity.verifyMetadata(
                "com.example.fake",
                13,
                List.of(ApkIntegrity.EXPECTED_CERTIFICATE_SHA256),
                13
        );
    }

    @Test(expected = SecurityException.class)
    public void rejectsAnotherVersion() {
        ApkIntegrity.verifyMetadata(
                ApkIntegrity.EXPECTED_PACKAGE,
                12,
                List.of(ApkIntegrity.EXPECTED_CERTIFICATE_SHA256),
                13
        );
    }

    @Test(expected = SecurityException.class)
    public void rejectsAnotherCertificate() {
        ApkIntegrity.verifyMetadata(
                ApkIntegrity.EXPECTED_PACKAGE,
                13,
                List.of("0".repeat(64)),
                13
        );
    }

    @Test(expected = SecurityException.class)
    public void rejectsMissingCertificate() {
        ApkIntegrity.verifyMetadata(
                ApkIntegrity.EXPECTED_PACKAGE,
                13,
                List.of(),
                13
        );
    }

    @Test(expected = SecurityException.class)
    public void rejectsMultipleCertificates() {
        ApkIntegrity.verifyMetadata(
                ApkIntegrity.EXPECTED_PACKAGE,
                13,
                List.of(
                        ApkIntegrity.EXPECTED_CERTIFICATE_SHA256,
                        ApkIntegrity.EXPECTED_CERTIFICATE_SHA256
                ),
                13
        );
    }

    @Test
    public void acceptsUppercaseCertificateHash() {
        ApkIntegrity.verifyMetadata(
                ApkIntegrity.EXPECTED_PACKAGE,
                13,
                List.of(ApkIntegrity.EXPECTED_CERTIFICATE_SHA256.toUpperCase(Locale.ROOT)),
                13
        );
    }
}
