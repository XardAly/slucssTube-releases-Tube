package com.xard.ytsystem.bootstrap;

import android.content.Context;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.Signature;
import android.os.Build;

import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

final class ApkIntegrity {
    static final String EXPECTED_PACKAGE = "com.xard.ytsystem";
    static final String EXPECTED_CERTIFICATE_SHA256 =
            "2475687690fc94a3d27ab6206f804f963a91ebb781d96ca67a223f805dd8a775";

    private ApkIntegrity() {
    }

    static void verifyCachedApk(
            Context context,
            File apk,
            ReleaseManifest manifest
    ) throws Exception {
        if (!manifest.download.sha256.equals(sha256(apk))) {
            throw new SecurityException("O APK salvo não corresponde à versão publicada.");
        }
        verifyArchive(context, apk, manifest.versionCode);
    }

    static void verifyArchive(Context context, File apk, int expectedVersionCode) {
        PackageManager manager = context.getPackageManager();
        int flags = Build.VERSION.SDK_INT >= Build.VERSION_CODES.P
                ? PackageManager.GET_SIGNING_CERTIFICATES
                : PackageManager.GET_SIGNATURES;
        PackageInfo info = manager.getPackageArchiveInfo(apk.getAbsolutePath(), flags);
        if (info == null) {
            throw new SecurityException("O arquivo baixado não é um APK Android válido.");
        }

        long versionCode = Build.VERSION.SDK_INT >= Build.VERSION_CODES.P
                ? info.getLongVersionCode()
                : info.versionCode;
        Signature[] signatures;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            signatures = info.signingInfo == null
                    ? null
                    : info.signingInfo.getApkContentsSigners();
        } else {
            signatures = info.signatures;
        }
        List<String> certificateHashes = new ArrayList<>();
        if (signatures != null) {
            for (Signature signature : signatures) {
                certificateHashes.add(sha256(signature.toByteArray()));
            }
        }
        verifyMetadata(info.packageName, versionCode, certificateHashes, expectedVersionCode);
    }

    static void verifyMetadata(
            String packageName,
            long versionCode,
            List<String> certificateHashes,
            int expectedVersionCode
    ) {
        if (!EXPECTED_PACKAGE.equals(packageName)) {
            throw new SecurityException("O APK pertence a outro aplicativo.");
        }
        if (versionCode != expectedVersionCode) {
            throw new SecurityException("O APK não corresponde à versão anunciada.");
        }
        if (certificateHashes == null
                || certificateHashes.size() != 1
                || !EXPECTED_CERTIFICATE_SHA256.equals(
                        certificateHashes.get(0).toLowerCase(Locale.ROOT)
                )) {
            throw new SecurityException("O APK não possui a assinatura oficial.");
        }
    }

    static String sha256(File file) throws IOException {
        try (FileInputStream input = new FileInputStream(file)) {
            MessageDigest digest = newDigest();
            byte[] buffer = new byte[256 * 1024];
            int read;
            while ((read = input.read(buffer)) != -1) {
                digest.update(buffer, 0, read);
            }
            return hex(digest.digest());
        }
    }

    static String sha256(byte[] value) {
        MessageDigest digest = newDigest();
        return hex(digest.digest(value));
    }

    private static MessageDigest newDigest() {
        try {
            return MessageDigest.getInstance("SHA-256");
        } catch (Exception error) {
            throw new IllegalStateException("SHA-256 indisponível.", error);
        }
    }

    static String hex(byte[] value) {
        StringBuilder result = new StringBuilder(value.length * 2);
        for (byte item : value) result.append(String.format(Locale.ROOT, "%02x", item));
        return result.toString();
    }
}
