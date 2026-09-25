package com.xard.ytsystem.update

import org.junit.Assert.*
import org.junit.Test

class ReleasePolicyTest {
    @Test fun aReleaseNotifiesOnceAndNeverOnRollbackOrSameCode() {
        assertTrue(ReleasePolicy.shouldNotify(22, 23, "android-23", "", 0))
        assertFalse(ReleasePolicy.shouldNotify(22, 23, "android-23", "android-23", 23))
        assertFalse(ReleasePolicy.shouldNotify(23, 23, "android-23", "", 0))
        assertFalse(ReleasePolicy.shouldNotify(22, 23, "changed-id", "android-23", 23))
        assertFalse(ReleasePolicy.shouldNotify(22, 23, "android-23", "android-24", 24))
        assertTrue(ReleasePolicy.shouldNotify(22, 24, "android-24", "android-23", 23))
    }
}
