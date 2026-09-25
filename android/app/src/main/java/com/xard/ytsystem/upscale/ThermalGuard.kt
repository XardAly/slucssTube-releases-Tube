package com.xard.ytsystem.upscale

import android.content.Context
import android.os.BatteryManager
import android.os.Build
import android.os.PowerManager
import java.io.Closeable
import java.util.concurrent.atomic.AtomicInteger

class ThermalGuard(private val context: Context) : Closeable {
    private val power = context.getSystemService(PowerManager::class.java)
    private val battery = context.getSystemService(BatteryManager::class.java)
    private val current = AtomicInteger(
        if (Build.VERSION.SDK_INT >= 29) power.currentThermalStatus else 0,
    )
    private val maximum = AtomicInteger(current.get())
    private val listener = if (Build.VERSION.SDK_INT >= 29) {
        PowerManager.OnThermalStatusChangedListener { status ->
            current.set(status)
            maximum.accumulateAndGet(status, ::maxOf)
        }
    } else {
        null
    }

    init {
        if (Build.VERSION.SDK_INT >= 29 && listener != null) {
            power.addThermalStatusListener(context.mainExecutor, listener)
        }
    }

    val batteryPercent: Int
        get() = battery.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)

    val maximumStatus: Int
        get() = maximum.get()

    fun beforeFrame() {
        if (Build.VERSION.SDK_INT < 29) return
        when {
            current.get() >= PowerManager.THERMAL_STATUS_CRITICAL -> throw ThermalCritical()
            current.get() >= PowerManager.THERMAL_STATUS_SEVERE -> Thread.sleep(750)
        }
    }

    override fun close() {
        if (Build.VERSION.SDK_INT >= 29 && listener != null) {
            power.removeThermalStatusListener(listener)
        }
    }
}

class ThermalCritical : Exception()
