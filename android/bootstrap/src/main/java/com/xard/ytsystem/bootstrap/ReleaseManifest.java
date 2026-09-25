package com.xard.ytsystem.bootstrap;

import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.google.gson.JsonPrimitive;

import java.net.URI;
import java.util.Locale;

final class ReleaseManifest {
    final String versionName;
    final int versionCode;
    final Download download;

    private ReleaseManifest(String versionName, int versionCode, Download download) {
        this.versionName = versionName;
        this.versionCode = versionCode;
        this.download = download;
    }

    static ReleaseManifest parseSigned(String json, String[] supportedAbis, int currentVersionCode) {
        JsonElement parsed;
        try {
            parsed = JsonParser.parseString(json);
        } catch (RuntimeException error) {
            throw new SecurityException("O servidor retornou um manifesto inválido.", error);
        }
        if (!parsed.isJsonObject()) {
            throw new SecurityException("O servidor retornou um manifesto inválido.");
        }
        JsonObject manifest = parsed.getAsJsonObject();
        String signature = requiredString(manifest, "signature");
        manifest.remove("signature");
        if (!ServerSignature.verify(manifest, signature)) {
            throw new SecurityException("A assinatura de segurança da atualização é inválida.");
        }

        String versionName = requiredString(manifest, "latest_version");
        int versionCode = requiredPositiveInt(manifest, "latest_version_code");
        if (versionCode <= currentVersionCode) {
            throw new SecurityException("A API não publicou uma versão completa mais recente.");
        }
        Download download = chooseDownload(
                manifest.getAsJsonObject("downloads"),
                supportedAbis,
                optionalString(manifest, "download_url"),
                optionalString(manifest, "sha256")
        );
        return new ReleaseManifest(versionName, versionCode, download);
    }

    static Download chooseDownload(
            JsonObject downloads,
            String[] supportedAbis,
            String universalUrl,
            String universalSha256
    ) {
        boolean supportsArm = false;
        if (supportedAbis != null) {
            for (String abi : supportedAbis) {
                if (!"arm64-v8a".equals(abi) && !"armeabi-v7a".equals(abi)) {
                    continue;
                }
                supportsArm = true;
                if (downloads == null || !downloads.has(abi) || !downloads.get(abi).isJsonObject()) {
                    continue;
                }
                JsonObject item = downloads.getAsJsonObject(abi);
                String url = optionalString(item, "url");
                String sha = optionalString(item, "sha256").toLowerCase(Locale.ROOT);
                if (isOfficialDownloadUrl(url) && isSha256(sha)) {
                    return new Download(url, sha, abi);
                }
            }
        }
        if (!supportsArm) {
            throw new SecurityException("Este aparelho não possui uma arquitetura Android compatível.");
        }
        String normalizedSha = universalSha256 == null
                ? ""
                : universalSha256.toLowerCase(Locale.ROOT);
        if (!isOfficialDownloadUrl(universalUrl) || !isSha256(normalizedSha)) {
            throw new SecurityException("Nenhum APK compatível foi publicado com segurança.");
        }
        return new Download(universalUrl, normalizedSha, "universal");
    }

    static boolean isSecureUrl(String value) {
        if (value == null || value.trim().isEmpty()) return false;
        try {
            URI uri = URI.create(value);
            return "https".equalsIgnoreCase(uri.getScheme())
                    && uri.getHost() != null
                    && !uri.getHost().trim().isEmpty()
                    && uri.getUserInfo() == null;
        } catch (IllegalArgumentException ignored) {
            return false;
        }
    }

    static boolean isOfficialDownloadUrl(String value) {
        if (!isSecureUrl(value)) return false;
        try {
            URI uri = URI.create(value);
            return "github.com".equalsIgnoreCase(uri.getHost())
                    && uri.getPath() != null
                    && uri.getPath().startsWith(
                            "/XardAly/slucssTube-releases-Tube/releases/download/"
                    );
        } catch (IllegalArgumentException ignored) {
            return false;
        }
    }

    static boolean isSha256(String value) {
        return value != null && value.matches("[0-9a-f]{64}");
    }

    private static String requiredString(JsonObject object, String key) {
        String value = optionalString(object, key);
        if (value.trim().isEmpty()) {
            throw new SecurityException("Campo obrigatório ausente no manifesto.");
        }
        return value;
    }

    private static String optionalString(JsonObject object, String key) {
        if (object == null) return "";
        JsonElement value = object.get(key);
        if (!(value instanceof JsonPrimitive) || !value.getAsJsonPrimitive().isString()) {
            return "";
        }
        return value.getAsString().trim();
    }

    private static int requiredPositiveInt(JsonObject object, String key) {
        try {
            JsonElement value = object.get(key);
            int parsed = value == null ? 0 : value.getAsInt();
            if (parsed > 0) return parsed;
        } catch (RuntimeException ignored) {
            // Tratado abaixo como manifesto inválido.
        }
        throw new SecurityException("Código de versão inválido no manifesto.");
    }

    static final class Download {
        final String url;
        final String sha256;
        final String abi;

        Download(String url, String sha256, String abi) {
            this.url = url;
            this.sha256 = sha256;
            this.abi = abi;
        }
    }
}
