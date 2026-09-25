package com.xard.ytsystem.download

/** Call from one producer. Stage changes always surface immediately. */
internal class ProgressThrottle(private val intervalMillis: Long = 750) {
    private var previous: Long? = null
    private var previousStage: String? = null

    fun shouldPublish(now: Long, stage: String, finished: Boolean = false): Boolean {
        val last = previous
        if (!finished && previousStage == stage && last != null && now - last < intervalMillis) return false
        previous = now
        previousStage = stage
        return true
    }
}
