package com.xard.ytsystem.identity

import org.junit.Assert.*
import org.junit.Test

class LocalNameTest {
    @Test fun normalizesWhitespaceAndRemovesInvisibleControlsWithoutLosingAccents() {
        assertEquals("João da Silva", LocalNameStore.normalize("  João   da\u00a0Silva  "))
        assertEquals("", LocalNameStore.normalize("\u200b\n\t\u00a0"))
        assertEquals("Ana", LocalNameStore.normalize("\u202eAna"))
    }
}
