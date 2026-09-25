package com.xard.ytsystem.update

import android.content.Context
import com.xard.ytsystem.api.AndroidManifest

class RemoteConfigStore(context: Context) {
    private val preferences = context.applicationContext
        .getSharedPreferences("remote_config", Context.MODE_PRIVATE)

    val requiresUpdate: Boolean
        get() = com.xard.ytsystem.BuildConfig.VERSION_CODE < preferences.getInt("minimum_version_code", 0) ||
            (preferences.getString("update_policy", "") == "mandatory" &&
                com.xard.ytsystem.BuildConfig.VERSION_CODE < preferences.getInt("latest_version_code", 0))

    val processingMode: String
        get() = preferences.getString("processing_mode", "local") ?: "local"
    val serverFallbackEnabled: Boolean
        get() = preferences.getBoolean("server_fallback_enabled", false)
    val upscaleEnabled: Boolean
        get() = preferences.getBoolean("upscale_enabled", true)
    val upscaleMinimumVersionCode: Int
        get() = preferences.getInt("upscale_minimum_version_code", 20)
    val upscaleEngineVersion: String
        get() = preferences.getString("upscale_engine_version", "").orEmpty()

    fun upscaleModelVersion(apiValue: String): String =
        preferences.getString("upscale_model_version_$apiValue", "").orEmpty()

    fun update(manifest: AndroidManifest) {
        preferences.edit()
            .putInt("minimum_version_code", manifest.minimumVersionCode)
            .putInt("latest_version_code", manifest.latestVersionCode)
            .putString("update_policy", manifest.updatePolicy)
            .putString("processing_mode", manifest.processingMode)
            .putBoolean("server_fallback_enabled", manifest.serverFallbackEnabled)
            .putBoolean("upscale_enabled", manifest.upscaleEnabled)
            .putInt("upscale_minimum_version_code", manifest.upscaleMinimumVersionCode)
            .putString("upscale_engine_version", manifest.upscaleEngineVersion)
            .putString(
                "upscale_model_version_realesr_animevideov3",
                manifest.animeVideoV3ModelVersion,
            )
            .putString(
                "upscale_model_version_realcugan",
                manifest.realCuganModelVersion,
            )
            .apply()
    }
}
