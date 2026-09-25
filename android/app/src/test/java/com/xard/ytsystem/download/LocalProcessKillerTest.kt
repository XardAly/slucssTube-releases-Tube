package com.xard.ytsystem.download

import org.junit.Assert.assertEquals
import org.junit.Test

class LocalProcessKillerTest {
    @Test
    fun listaDeFilhosAceitaEspacosEIgnoraValoresInvalidos() {
        assertEquals(listOf(120, 121, 122), LocalProcessKiller.parseChildren("120  121\n122 x -1 120"))
    }
}
