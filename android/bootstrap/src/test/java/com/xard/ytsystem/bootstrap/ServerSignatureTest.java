package com.xard.ytsystem.bootstrap;

import com.google.gson.JsonElement;
import com.google.gson.JsonParser;

import org.junit.Test;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class ServerSignatureTest {
    @Test
    public void canonicalJsonSortsKeysRecursively() {
        JsonElement value = JsonParser.parseString(
                "{\"z\":2,\"a\":{\"y\":true,\"b\":\"ok\"},\"list\":[2,1]}"
        );

        assertEquals(
                "{\"a\":{\"b\":\"ok\",\"y\":true},\"list\":[2,1],\"z\":2}",
                ServerSignature.canonical(value)
        );
    }

    @Test
    public void embeddedPublicKeyAcceptsBackendSignatureAndRejectsTampering() {
        String signature =
                "weZaPOTrrnZHXYXh8WDZ8HJmapdr/CodgMsDAay0y0KQaHfwwGEGeD8JOwnIpbdQwuDxMrr5fS9OLxN6iLklBA==";

        assertTrue(ServerSignature.verify(
                JsonParser.parseString("{\"z\":true,\"a\":1}"),
                signature
        ));
        assertFalse(ServerSignature.verify(
                JsonParser.parseString("{\"z\":false,\"a\":1}"),
                signature
        ));
    }
}
