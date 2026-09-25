package com.xard.ytsystem.bootstrap;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import com.google.gson.JsonPrimitive;

import net.i2p.crypto.eddsa.EdDSAEngine;
import net.i2p.crypto.eddsa.EdDSAPublicKey;
import net.i2p.crypto.eddsa.spec.EdDSANamedCurveTable;
import net.i2p.crypto.eddsa.spec.EdDSAPublicKeySpec;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Collections;
import java.util.List;
import java.util.Map;

final class ServerSignature {
    private static final String PUBLIC_KEY_DER_BASE64 =
            "MCowBQYDK2VwAyEA4ThjCx+x4hI62veG6CIRAPK3cQgU97/TFiC/KV/mJqw=";

    private ServerSignature() {
    }

    static boolean verify(JsonElement element, String signatureBase64) {
        try {
            byte[] der = Base64.getDecoder().decode(PUBLIC_KEY_DER_BASE64);
            byte[] raw = new byte[32];
            System.arraycopy(der, der.length - raw.length, raw, 0, raw.length);
            EdDSAPublicKeySpec keySpec = new EdDSAPublicKeySpec(
                    raw,
                    EdDSANamedCurveTable.getByName("Ed25519")
            );
            EdDSAEngine verifier = new EdDSAEngine(MessageDigest.getInstance("SHA-512"));
            verifier.initVerify(new EdDSAPublicKey(keySpec));
            verifier.update(canonical(element).getBytes(StandardCharsets.UTF_8));
            return verifier.verify(Base64.getDecoder().decode(signatureBase64));
        } catch (Exception ignored) {
            return false;
        }
    }

    static String canonical(JsonElement element) {
        if (element == null || element instanceof JsonNull) {
            return "null";
        }
        if (element instanceof JsonArray) {
            StringBuilder result = new StringBuilder("[");
            boolean first = true;
            for (JsonElement item : element.getAsJsonArray()) {
                if (!first) result.append(',');
                result.append(canonical(item));
                first = false;
            }
            return result.append(']').toString();
        }
        if (element instanceof JsonObject) {
            List<String> keys = new ArrayList<>();
            for (Map.Entry<String, JsonElement> entry : element.getAsJsonObject().entrySet()) {
                keys.add(entry.getKey());
            }
            Collections.sort(keys);
            StringBuilder result = new StringBuilder("{");
            for (int index = 0; index < keys.size(); index++) {
                if (index > 0) result.append(',');
                String key = keys.get(index);
                result.append(new JsonPrimitive(key)).append(':')
                        .append(canonical(element.getAsJsonObject().get(key)));
            }
            return result.append('}').toString();
        }
        return element.toString();
    }
}
