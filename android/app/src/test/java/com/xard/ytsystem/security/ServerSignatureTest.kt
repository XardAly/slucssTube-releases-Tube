package com.xard.ytsystem.security

import com.google.gson.JsonParser
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ServerSignatureTest {
    @Test
    fun jsonCanonicoOrdenaChavesRecursivamente() {
        val value = JsonParser.parseString("""{"z":2,"a":{"y":true,"b":"ok"},"list":[2,1]}""")
        assertEquals(
            """{"a":{"b":"ok","y":true},"list":[2,1],"z":2}""",
            ServerSignature.canonical(value),
        )
    }

    @Test
    fun chavePublicaEmbutidaConfereComAssinaturaDoBackend() {
        val value = JsonParser.parseString("""{"z":true,"a":1}""")
        assertTrue(
            ServerSignature.verify(
                value,
                "weZaPOTrrnZHXYXh8WDZ8HJmapdr/CodgMsDAay0y0KQaHfwwGEGeD8JOwnIpbdQwuDxMrr5fS9OLxN6iLklBA==",
            ),
        )
    }
}
