package com.xard.ytsystem.security

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

class SecureStore(context: Context) {
    private val preferences = context.applicationContext
        .getSharedPreferences("secure_state", Context.MODE_PRIVATE)

    fun put(key: String, value: ByteArray?) {
        if (value == null) {
            preferences.edit().remove(key).apply()
            return
        }
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, getOrCreateKey())
        val encrypted = cipher.doFinal(value)
        val packed = ByteArray(1 + cipher.iv.size + encrypted.size)
        packed[0] = cipher.iv.size.toByte()
        cipher.iv.copyInto(packed, 1)
        encrypted.copyInto(packed, 1 + cipher.iv.size)
        preferences.edit().putString(key, Base64.encodeToString(packed, Base64.NO_WRAP)).apply()
    }

    fun get(key: String): ByteArray? {
        val encoded = preferences.getString(key, null) ?: return null
        return try {
            val packed = Base64.decode(encoded, Base64.NO_WRAP)
            val ivLength = packed.first().toInt() and 0xff
            require(ivLength in 12..16 && packed.size > ivLength + 1)
            val iv = packed.copyOfRange(1, 1 + ivLength)
            val cipherText = packed.copyOfRange(1 + ivLength, packed.size)
            Cipher.getInstance(TRANSFORMATION).run {
                init(Cipher.DECRYPT_MODE, getOrCreateKey(), GCMParameterSpec(128, iv))
                doFinal(cipherText)
            }
        } catch (_: Exception) {
            null
        }
    }

    fun clear(vararg keys: String) {
        preferences.edit().also { editor -> keys.forEach(editor::remove) }.apply()
    }

    private fun getOrCreateKey(): SecretKey {
        val keyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (keyStore.getKey(KEY_ALIAS, null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").run {
            init(
                KeyGenParameterSpec.Builder(
                    KEY_ALIAS,
                    KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
                )
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .setKeySize(256)
                    .build(),
            )
            generateKey()
        }
    }

    companion object {
        private const val KEY_ALIAS = "slucss-device-state-v1"
        private const val TRANSFORMATION = "AES/GCM/NoPadding"
    }
}
