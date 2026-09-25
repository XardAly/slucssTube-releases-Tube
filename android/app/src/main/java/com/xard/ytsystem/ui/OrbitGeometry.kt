package com.xard.ytsystem.ui

import kotlin.math.cos
import kotlin.math.sin

object OrbitGeometry {
    data class Point(val x: Float, val y: Float, val depth: Float)

    fun point(angle: Double, radiusX: Float, radiusY: Float, tilt: Double): Point {
        val x = radiusX * cos(angle)
        val y = radiusY * sin(angle)
        return Point((x * cos(tilt) - y * sin(tilt)).toFloat(),
            (x * sin(tilt) + y * cos(tilt)).toFloat(), sin(angle).toFloat())
    }
}
