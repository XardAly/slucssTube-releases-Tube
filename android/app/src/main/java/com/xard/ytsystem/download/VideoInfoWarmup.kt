package com.xard.ytsystem.download

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.withTimeoutOrNull
import java.security.MessageDigest
import java.util.concurrent.ConcurrentHashMap

/** Coordena a análise local entre a tela e o serviço sem reter Context/Activity. */
internal object VideoInfoWarmup {
    private class Entry {
        val completed = CompletableDeferred<Unit>()
    }

    internal class Lease internal constructor(
        private val key: String,
        private val token: Any,
        private val completed: CompletableDeferred<Unit>,
        val isOwner: Boolean,
    ) {
        suspend fun await(timeoutMillis: Long): Boolean =
            withTimeoutOrNull(timeoutMillis) {
                completed.await()
                true
            } ?: false

        fun finish() {
            if (!isOwner) return
            ENTRIES.remove(key, token)
            completed.complete(Unit)
        }
    }

    fun begin(url: String): Lease {
        val key = key(url)
        val created = Entry()
        val existing = ENTRIES.putIfAbsent(key, created)
        val active = existing ?: created
        return Lease(key, active, active.completed, existing == null)
    }

    suspend fun await(url: String, timeoutMillis: Long): Boolean {
        // Ausência também significa que o trabalho terminou entre a checagem
        // do chamador e esta linha; isso evita perder um cache recém-promovido.
        val entry = ENTRIES[key(url)] ?: return true
        return withTimeoutOrNull(timeoutMillis) {
            entry.completed.await()
            true
        } ?: false
    }

    fun isActive(url: String): Boolean = ENTRIES.containsKey(key(url))

    internal fun activeCountForTesting(): Int = ENTRIES.size

    private fun key(url: String): String = MessageDigest.getInstance("SHA-256")
        .digest(url.toByteArray(Charsets.UTF_8))
        .joinToString("") { "%02x".format(it) }

    private val ENTRIES = ConcurrentHashMap<String, Entry>()
}
