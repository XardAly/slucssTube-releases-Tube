package com.xard.ytsystem.identity

import android.content.Context

/** Only a local display name. Never added to sessions, jobs or device registration. */
class LocalNameStore(context: Context) {
    private val preferences = context.applicationContext.getSharedPreferences("local_name", Context.MODE_PRIVATE)
    val name: String get() = preferences.getString("name", "").orEmpty()

    fun save(value: String) {
        val normalized = normalize(value)
        require(normalized.isNotEmpty() && normalized.length <= MAX_LENGTH)
        preferences.edit().putString("name", normalized).apply()
    }

    companion object {
        const val MAX_LENGTH = 60
        fun normalize(value: String): String = value
            .filterNot { Character.isISOControl(it) || Character.getType(it) == Character.FORMAT.toInt() }
            .trim().replace(Regex("[\\s\\p{Z}]+"), " ").trim()
    }
}
