package com.xard.ytsystem.download

import android.os.Process
import com.yausername.youtubedl_android.YoutubeDL
import java.io.File

object LocalProcessKiller {
    fun destroyYoutubeDl(processId: String) {
        val process = findYoutubeDlProcess(processId)
        if (process != null) {
            val descendants = processPid(process)?.let(::descendantsOf).orEmpty()
            descendants.asReversed().forEach { pid ->
                runCatching { Process.killProcess(pid) }
            }
            runCatching { process.destroy() }
        }
        // Remove também o registro interno e preserva o comportamento
        // oficial da biblioteca caso sua implementação seja corrigida.
        runCatching { YoutubeDL.getInstance().destroyProcessById(processId) }
    }

    internal fun parseChildren(value: String): List<Int> = value
        .split(Regex("\\s+"))
        .mapNotNull { it.toIntOrNull() }
        .filter { it > 0 }
        .distinct()
        .take(MAX_PROCESSES)

    private fun descendantsOf(parentPid: Int): List<Int> {
        val found = mutableListOf<Int>()
        val pending = ArrayDeque<Int>()
        pending.add(parentPid)
        while (pending.isNotEmpty() && found.size < MAX_PROCESSES) {
            val pid = pending.removeFirst()
            val children = runCatching {
                parseChildren(File("/proc/$pid/task/$pid/children").readText())
            }.getOrDefault(emptyList())
            children.filterNot(found::contains).forEach {
                found += it
                pending += it
            }
        }
        return found
    }

    @Suppress("UNCHECKED_CAST")
    private fun findYoutubeDlProcess(processId: String): java.lang.Process? = runCatching {
        val field = YoutubeDL::class.java.getDeclaredField("idProcessMap")
        field.isAccessible = true
        val map = field.get(YoutubeDL) as Map<String, java.lang.Process>
        synchronized(map) { map[processId] }
    }.getOrNull()

    private fun processPid(process: java.lang.Process): Int? = runCatching {
        generateSequence(process.javaClass as Class<*>?) { it.superclass }
            .mapNotNull { type ->
                runCatching { type.getDeclaredField("pid") }.getOrNull()
            }
            .firstOrNull()
            ?.apply { isAccessible = true }
            ?.get(process)
            .let { (it as? Number)?.toInt() }
    }.getOrNull()

    private const val MAX_PROCESSES = 256
}
