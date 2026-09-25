package com.xard.ytsystem.ui

import android.animation.ValueAnimator
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.database.ContentObserver
import android.graphics.*
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.provider.Settings
import android.util.AttributeSet
import android.view.View
import android.view.ViewTreeObserver
import androidx.core.content.ContextCompat
import com.xard.ytsystem.R
import kotlin.math.PI
import kotlin.math.min

/** Separable raster subject with real projected orbits, front/back occlusion and metallic satellites. */
class OrbitArtworkView @JvmOverloads constructor(context: Context, attrs: AttributeSet? = null) : View(context, attrs) {
    private val subject = BitmapFactory.decodeResource(resources, R.drawable.studio_play_layer).apply { setHasMipMap(true) }
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG or Paint.FILTER_BITMAP_FLAG)
    private val subjectBounds = RectF()
    private val visibleBounds = Rect()
    private val backPaths = arrayOf(Path(), Path())
    private val frontPaths = arrayOf(Path(), Path())
    private val tilts = doubleArrayOf(-.31, .38)
    private val sphere = RadialGradient(-.32f, -.4f, 1.35f,
        intArrayOf(Color.WHITE, Color.rgb(111, 101, 103), Color.rgb(30, 17, 20), Color.rgb(164, 33, 45)),
        floatArrayOf(0f, .2f, .72f, 1f), Shader.TileMode.CLAMP)
    private val power by lazy { context.getSystemService(PowerManager::class.java) }
    private var radiusX = 0f
    private var radiusY = 0f
    private var lastFrame = 0L
    private var scheduled = false
    var orbitPhase = 0.6
        private set
    var isMotionPaused = false
        private set
    private val frame = object : Runnable {
        override fun run() {
            scheduled = false
            if (!canAnimate()) { lastFrame = 0; return }
            val now = SystemClock.uptimeMillis()
            // Keep a continuous phase: different orbital speeds must not jump at a 2π wrap.
            if (lastFrame != 0L) orbitPhase += (now - lastFrame) * 2 * PI / 9000
            lastFrame = now
            invalidate()
            scheduled = true
            postDelayed(this, 33) // decorative scene capped at 30fps
        }
    }
    private val scrollListener = ViewTreeObserver.OnScrollChangedListener { syncMotion() }
    private val settingObserver = object : ContentObserver(Handler(Looper.getMainLooper())) {
        override fun onChange(selfChange: Boolean) { syncMotion() }
    }
    private val powerReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) { syncMotion() }
    }

    init { importantForAccessibility = IMPORTANT_FOR_ACCESSIBILITY_NO }

    fun setMotionPaused(paused: Boolean) {
        isMotionPaused = paused
        syncMotion()
    }

    private fun canAnimate() = isAttachedToWindow && isShown && hasWindowFocus() && windowVisibility == VISIBLE &&
        !isMotionPaused && ValueAnimator.areAnimatorsEnabled() && !power.isPowerSaveMode && getLocalVisibleRect(visibleBounds)

    private fun syncMotion() {
        if (!isAttachedToWindow) return
        if (canAnimate()) {
            if (!scheduled) { scheduled = true; post(frame) }
        } else {
            removeCallbacks(frame)
            scheduled = false
            lastFrame = 0
        }
    }

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        val size = h * .98f
        subjectBounds.set((w - size) / 2, (h - size) / 2, (w + size) / 2, (h + size) / 2)
        radiusX = min(w * .43f, h * .92f)
        radiusY = h * .17f
        for (ring in 0..1) {
            backPaths[ring].reset(); frontPaths[ring].reset()
            for (half in 0..1) {
                val path = if (half == 0) frontPaths[ring] else backPaths[ring]
                for (step in 0..90) {
                    val angle = half * PI + step * PI / 90
                    val p = OrbitGeometry.point(angle, radiusX, radiusY, tilts[ring])
                    if (step == 0) path.moveTo(p.x, p.y) else path.lineTo(p.x, p.y)
                }
            }
        }
        syncMotion()
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        canvas.save()
        canvas.translate(width / 2f, height / 2f)
        drawRings(canvas, false)
        drawSatellites(canvas, false)
        canvas.restore()
        paint.shader = null; paint.style = Paint.Style.FILL; paint.alpha = 255
        canvas.drawBitmap(subject, null, subjectBounds, paint)
        canvas.save()
        canvas.translate(width / 2f, height / 2f)
        drawRings(canvas, true)
        drawSatellites(canvas, true)
        canvas.restore()
    }

    private fun drawRings(canvas: Canvas, front: Boolean) {
        paint.shader = null
        paint.style = Paint.Style.STROKE
        paint.strokeWidth = resources.displayMetrics.density * .7f
        paint.color = if (front) Color.rgb(159, 77, 83) else Color.rgb(67, 39, 43)
        for (path in if (front) frontPaths else backPaths) canvas.drawPath(path, paint)
    }

    private fun drawSatellites(canvas: Canvas, front: Boolean) {
        for (ring in 0..1) {
            val angle = if (ring == 0) orbitPhase else -orbitPhase * 1.35 + PI
            val point = OrbitGeometry.point(angle, radiusX, radiusY, tilts[ring])
            if ((point.depth >= 0) != front) continue
            val radius = resources.displayMetrics.density * (4.8f + 1.6f * point.depth)
            canvas.save()
            canvas.translate(point.x, point.y)
            canvas.scale(radius, radius)
            paint.style = Paint.Style.FILL; paint.shader = sphere; paint.alpha = if (front) 255 else 175
            canvas.drawCircle(0f, 0f, 1f, paint)
            paint.shader = null; paint.style = Paint.Style.STROKE; paint.strokeWidth = .12f
            paint.color = Color.rgb(225, 55, 69); paint.alpha = if (front) 220 else 110
            canvas.drawCircle(0f, 0f, 1f, paint)
            paint.alpha = 255
            canvas.restore()
        }
    }

    override fun onAttachedToWindow() {
        super.onAttachedToWindow()
        viewTreeObserver.addOnScrollChangedListener(scrollListener)
        context.contentResolver.registerContentObserver(Settings.Global.getUriFor(Settings.Global.ANIMATOR_DURATION_SCALE), false, settingObserver)
        ContextCompat.registerReceiver(context, powerReceiver, IntentFilter(PowerManager.ACTION_POWER_SAVE_MODE_CHANGED), ContextCompat.RECEIVER_NOT_EXPORTED)
        syncMotion()
    }

    override fun onWindowFocusChanged(hasWindowFocus: Boolean) { super.onWindowFocusChanged(hasWindowFocus); syncMotion() }
    override fun onVisibilityChanged(changedView: View, visibility: Int) { super.onVisibilityChanged(changedView, visibility); syncMotion() }
    override fun onWindowVisibilityChanged(visibility: Int) { super.onWindowVisibilityChanged(visibility); syncMotion() }
    override fun onDetachedFromWindow() {
        removeCallbacks(frame); scheduled = false; lastFrame = 0
        viewTreeObserver.removeOnScrollChangedListener(scrollListener)
        context.contentResolver.unregisterContentObserver(settingObserver)
        context.unregisterReceiver(powerReceiver)
        super.onDetachedFromWindow()
    }
}
