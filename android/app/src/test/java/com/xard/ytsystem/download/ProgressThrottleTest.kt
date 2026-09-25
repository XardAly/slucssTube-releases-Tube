package com.xard.ytsystem.download

import org.junit.Assert.*
import org.junit.Test

class ProgressThrottleTest {
    @Test fun busyFrameStreamPublishesOnlyAtIntervalsButPreservesStagesAndCompletion() {
        val throttle = ProgressThrottle()
        val accepted = (0L..3000L step 10).count { throttle.shouldPublish(it, "processing") }
        assertEquals(5, accepted)
        assertTrue(throttle.shouldPublish(3001, "saving"))
        assertFalse(throttle.shouldPublish(3002, "saving"))
        assertTrue(throttle.shouldPublish(3003, "saving", finished = true))
    }
}
