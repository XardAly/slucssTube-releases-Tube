package com.xard.ytsystem.security

import android.content.Context
import net.i2p.crypto.eddsa.EdDSAEngine
import net.i2p.crypto.eddsa.EdDSAPrivateKey
import net.i2p.crypto.eddsa.EdDSAPublicKey
import net.i2p.crypto.eddsa.KeyPairGenerator
import net.i2p.crypto.eddsa.spec.EdDSAGenParameterSpec
import net.i2p.crypto.eddsa.spec.EdDSANamedCurveTable
import net.i2p.crypto.eddsa.spec.EdDSAPrivateKeySpec
import net.i2p.crypto.eddsa.spec.EdDSAPublicKeySpec
import java.security.KeyPair
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.UUID
import java.util.Base64

class DeviceIdentity(context: Context) {
    private val preferences = context.applicationContext
        .getSharedPreferences("device_identity", Context.MODE_PRIVATE)
    private val secureStore = SecureStore(context)
    private val keyPair: KeyPair

    val deviceId: String
    val installationId: String
    val publicKeyPem: String

    init {
        val storedSeed = secureStore.get(KEY_SEED)
        val validIds = preferences.getString(KEY_DEVICE_ID, null) to
            preferences.getString(KEY_INSTALLATION_ID, null)
        if (storedSeed?.size == 32 && validIds.first != null && validIds.second != null) {
            keyPair = pairFromSeed(storedSeed)
            deviceId = validIds.first!!
            installationId = validIds.second!!
        } else {
            val seed = ByteArray(32).also(SecureRandom()::nextBytes)
            keyPair = pairFromSeed(seed)
            deviceId = UUID.randomUUID().toString()
            installationId = UUID.randomUUID().toString()
            secureStore.put(KEY_SEED, seed)
            preferences.edit()
                .putString(KEY_DEVICE_ID, deviceId)
                .putString(KEY_INSTALLATION_ID, installationId)
                .apply()
        }
        publicKeyPem = publicPem(keyPair.public as EdDSAPublicKey)
    }

    fun sign(message: ByteArray): String {
        val signer = EdDSAEngine(MessageDigest.getInstance("SHA-512"))
        signer.initSign(keyPair.private as EdDSAPrivateKey)
        signer.update(message)
        return Base64.getEncoder().encodeToString(signer.sign())
    }

    private fun pairFromSeed(seed: ByteArray): KeyPair {
        val spec = EdDSANamedCurveTable.getByName("Ed25519")
        val privateSpec = EdDSAPrivateKeySpec(seed, spec)
        val privateKey = EdDSAPrivateKey(privateSpec)
        val publicKey = EdDSAPublicKey(EdDSAPublicKeySpec(privateSpec.a, spec))
        return KeyPair(publicKey, privateKey)
    }

    private fun publicPem(key: EdDSAPublicKey): String {
        val spkiPrefix = byteArrayOf(
            0x30, 0x2a, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x03, 0x21, 0x00,
        )
        val der = spkiPrefix + key.abyte
        val base64 = Base64.getEncoder().encodeToString(der)
        return "-----BEGIN PUBLIC KEY-----\n$base64\n-----END PUBLIC KEY-----\n"
    }

    companion object {
        private const val KEY_SEED = "ed25519_seed"
        private const val KEY_DEVICE_ID = "device_id"
        private const val KEY_INSTALLATION_ID = "installation_id"
    }
}
