package com.xard.ytsystem.upscale

import android.app.ActivityManager
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import kotlin.math.min

data class DeviceCapabilities(
    val abis: List<String>,
    val hasVulkanFeature: Boolean,
    val gpu: NativeGpuInfo,
    val totalRamMb: Long,
    val availableRamMb: Long,
    val lowMemory: Boolean,
) {
    val canUseVulkan: Boolean
        get() = hasVulkanFeature && gpu.gpuCount > 0

    fun tileSize(model: UpscaleModel, scale: Int): Int {
        if (!canUseVulkan) return 64
        val heap = gpu.heapBudgetMb
        return if (model == UpscaleModel.ANIME_VIDEO_V3) {
            when {
                heap > 1_900 -> 200
                heap > 550 -> 100
                heap > 190 -> 64
                else -> 32
            }
        } else {
            when (scale) {
                2 -> when {
                    heap > 800 -> 300
                    heap > 400 -> 200
                    heap > 200 -> 100
                    else -> 32
                }
                3 -> when {
                    heap > 950 -> 200
                    heap > 320 -> 100
                    else -> 32
                }
                else -> when {
                    heap > 980 -> 300
                    heap > 530 -> 200
                    heap > 240 -> 100
                    else -> 32
                }
            }
        }
    }

    val inferenceThreads: Int
        get() = min(4, Runtime.getRuntime().availableProcessors().coerceAtLeast(1))

    companion object {
        fun inspect(context: Context): DeviceCapabilities {
            val memory = ActivityManager.MemoryInfo()
            context.getSystemService(ActivityManager::class.java).getMemoryInfo(memory)
            val hasVulkan = context.packageManager.hasSystemFeature(
                PackageManager.FEATURE_VULKAN_HARDWARE_LEVEL,
            )
            return DeviceCapabilities(
                abis = Build.SUPPORTED_ABIS?.toList().orEmpty(),
                hasVulkanFeature = hasVulkan,
                gpu = NativeUpscaler.gpuInfo(),
                totalRamMb = memory.totalMem / 1024 / 1024,
                availableRamMb = memory.availMem / 1024 / 1024,
                lowMemory = memory.lowMemory,
            )
        }
    }
}
