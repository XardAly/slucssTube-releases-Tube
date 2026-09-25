package com.xard.ytsystem.security

import com.google.gson.JsonArray
import com.google.gson.JsonElement
import com.google.gson.JsonNull
import com.google.gson.JsonObject
import com.google.gson.JsonPrimitive
import net.i2p.crypto.eddsa.EdDSAEngine
import net.i2p.crypto.eddsa.EdDSAPublicKey
import net.i2p.crypto.eddsa.spec.EdDSANamedCurveTable
import net.i2p.crypto.eddsa.spec.EdDSAPublicKeySpec
import java.security.MessageDigest
import java.util.Base64

object ServerSignature {
    private const val PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEA4ThjCx+x4hI62veG6CIRAPK3cQgU97/TFiC/KV/mJqw=
-----END PUBLIC KEY-----"""

    private val publicKey: EdDSAPublicKey by lazy {
        val der = Base64.getDecoder().decode(
            PUBLIC_KEY_PEM.lineSequence().filterNot { it.startsWith("---") }.joinToString(""),
        )
        val raw = der.copyOfRange(der.size - 32, der.size)
        val spec = EdDSANamedCurveTable.getByName("Ed25519")
        EdDSAPublicKey(EdDSAPublicKeySpec(raw, spec))
    }

    fun verify(element: JsonElement, signatureBase64: String): Boolean = try {
        val verifier = EdDSAEngine(MessageDigest.getInstance("SHA-512"))
        verifier.initVerify(publicKey)
        verifier.update(canonical(element).toByteArray(Charsets.UTF_8))
        verifier.verify(Base64.getDecoder().decode(signatureBase64))
    } catch (_: Exception) {
        false
    }

    fun canonical(element: JsonElement): String = when {
        element is JsonNull -> "null"
        element is JsonArray -> element.joinToString(prefix = "[", postfix = "]", separator = ",") {
            canonical(it)
        }
        element is JsonObject -> element.entrySet().sortedBy { it.key }.joinToString(
            prefix = "{", postfix = "}", separator = ",",
        ) { (key, value) -> "${quote(key)}:${canonical(value)}" }
        element is JsonPrimitive && element.isString -> quote(element.asString)
        else -> element.toString()
    }

    private fun quote(value: String): String = JsonPrimitive(value).toString()
}
