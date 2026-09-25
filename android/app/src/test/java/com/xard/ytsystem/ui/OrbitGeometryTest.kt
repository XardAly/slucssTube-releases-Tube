package com.xard.ytsystem.ui

import org.junit.Assert.*
import org.junit.Test
import kotlin.math.PI

class OrbitGeometryTest {
    @Test fun orbitClosesAndUsesOppositeDepths() {
        val a = OrbitGeometry.point(.7, 120f, 28f, -.3)
        val b = OrbitGeometry.point(.7 + PI * 2, 120f, 28f, -.3)
        assertEquals(a.x, b.x, .001f); assertEquals(a.y, b.y, .001f)
        val opposite = OrbitGeometry.point(.7 + PI, 120f, 28f, -.3)
        assertEquals(-a.x, opposite.x, .001f); assertEquals(-a.y, opposite.y, .001f)
        assertTrue(a.depth > 0); assertTrue(opposite.depth < 0)
    }
}
