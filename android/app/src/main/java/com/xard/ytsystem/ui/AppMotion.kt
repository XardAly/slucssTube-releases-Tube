package com.xard.ytsystem.ui

import android.animation.ValueAnimator
import android.content.Context
import android.os.PowerManager
import android.view.View
import android.view.animation.DecelerateInterpolator

/** Short, finite transitions: no decoder, timer or work while the screen is idle. */
object AppMotion {
    fun enabled(context: Context): Boolean = ValueAnimator.areAnimatorsEnabled() &&
        !context.getSystemService(PowerManager::class.java).isPowerSaveMode &&
        !context.getSharedPreferences("studio_visuals", Context.MODE_PRIVATE)
            .getBoolean("motion_paused", false)

    fun enter(view: View) {
        reset(view)
        if (!enabled(view.context)) return
        view.alpha = 0f
        view.translationY = 10 * view.resources.displayMetrics.density
        view.animate().alpha(1f).translationY(0f).setDuration(180)
            .setInterpolator(DecelerateInterpolator()).start()
    }

    fun reset(view: View) {
        view.animate().cancel()
        view.alpha = 1f
        view.translationY = 0f
    }
}
