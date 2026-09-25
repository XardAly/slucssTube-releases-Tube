package com.xard.ytsystem.ui

import android.animation.ValueAnimator
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.database.ContentObserver
import android.graphics.SurfaceTexture
import android.media.MediaPlayer
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.util.AttributeSet
import android.view.Surface
import android.view.TextureView
import android.view.View
import android.view.ViewTreeObserver
import android.widget.FrameLayout
import android.widget.ImageView
import androidx.core.content.ContextCompat
import com.xard.ytsystem.R

/** Local, silent hero loop. A poster stays visible until a decoded frame is available. */
class StudioHeroView @JvmOverloads constructor(context: Context, attrs: AttributeSet? = null) : FrameLayout(context, attrs), TextureView.SurfaceTextureListener {
    private val poster = ImageView(context).apply {
        setImageResource(R.drawable.studio_play_layer)
        scaleType = ImageView.ScaleType.FIT_CENTER
        val inset = (12 * resources.displayMetrics.density).toInt()
        setPadding(inset, inset, inset, inset)
    }
    private val texture = TextureView(context).apply { isOpaque = false; alpha = 0f }
    private var player: MediaPlayer? = null
    private var surface: Surface? = null
    private var prepared = false
    private var failed = false
    private var resumePosition = 0
    private val power by lazy { context.getSystemService(PowerManager::class.java) }
    private val visibleRect = android.graphics.Rect()
    val hasVideo = runCatching { context.assets.openFd("studio/hero.mp4").use { true } }.getOrDefault(false)
    var isMotionPaused = false
        private set
    val isPlaying: Boolean get() = prepared && player?.isPlaying == true
    private val scrollListener = ViewTreeObserver.OnScrollChangedListener { syncPlayback() }
    private val observer = object : ContentObserver(Handler(Looper.getMainLooper())) {
        override fun onChange(selfChange: Boolean) = syncPlayback()
    }
    private val powerReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) = syncPlayback()
    }

    init {
        importantForAccessibility = IMPORTANT_FOR_ACCESSIBILITY_NO_HIDE_DESCENDANTS
        addView(poster, LayoutParams(LayoutParams.MATCH_PARENT, LayoutParams.MATCH_PARENT))
        addView(texture, LayoutParams(LayoutParams.MATCH_PARENT, LayoutParams.MATCH_PARENT))
        texture.surfaceTextureListener = this
    }

    fun setMotionPaused(paused: Boolean) { isMotionPaused = paused; syncPlayback() }

    private fun canPlay() = isAttachedToWindow && isShown && hasWindowFocus() && windowVisibility == VISIBLE &&
        !isMotionPaused && ValueAnimator.areAnimatorsEnabled() && !power.isPowerSaveMode && getLocalVisibleRect(visibleRect)

    private fun syncPlayback() {
        if (!isAttachedToWindow || !hasVideo || failed) return
        if (!isShown || !hasWindowFocus() || windowVisibility != VISIBLE || !getLocalVisibleRect(visibleRect)) {
            releasePlayer()
            return
        }
        if (!canPlay()) {
            if (isPlaying) player?.pause()
            return
        }
        if (player == null && texture.isAvailable) preparePlayer()
        else if (prepared) player?.start()
    }

    private fun preparePlayer() {
        val surfaceTexture = texture.surfaceTexture ?: return
        val next = MediaPlayer()
        player = next
        try {
            surface = Surface(surfaceTexture)
            next.setSurface(surface)
            next.setVolume(0f, 0f)
            next.isLooping = true
            context.assets.openFd("studio/hero.mp4").use { next.setDataSource(it.fileDescriptor, it.startOffset, it.length) }
            next.setOnPreparedListener {
                if (player !== it) return@setOnPreparedListener
                prepared = true
                fitVideo(it.videoWidth, it.videoHeight)
                if (resumePosition > 0) it.seekTo(resumePosition)
                syncPlayback()
            }
            next.setOnInfoListener { _, what, _ ->
                if (what == MediaPlayer.MEDIA_INFO_VIDEO_RENDERING_START) {
                    texture.alpha = 1f
                    poster.visibility = INVISIBLE
                }
                false
            }
            next.setOnErrorListener { _, _, _ -> failed = true; releasePlayer(); true }
            next.prepareAsync()
        } catch (_: Exception) { failed = true; releasePlayer() }
    }

    private fun fitVideo(videoWidth: Int, videoHeight: Int) {
        if (videoWidth <= 0 || videoHeight <= 0 || width == 0 || height == 0) return
        val scale = minOf(width.toFloat() / videoWidth, height.toFloat() / videoHeight)
        texture.setTransform(android.graphics.Matrix().apply {
            setScale(videoWidth * scale / width, videoHeight * scale / height, width / 2f, height / 2f)
        })
    }

    private fun releasePlayer() {
        if (prepared) resumePosition = runCatching { player?.currentPosition ?: 0 }.getOrDefault(0)
        prepared = false
        player?.release(); player = null
        surface?.release(); surface = null
        texture.alpha = 0f
        poster.visibility = VISIBLE
    }

    override fun onAttachedToWindow() {
        super.onAttachedToWindow()
        viewTreeObserver.addOnScrollChangedListener(scrollListener)
        context.contentResolver.registerContentObserver(Settings.Global.getUriFor(Settings.Global.ANIMATOR_DURATION_SCALE), false, observer)
        ContextCompat.registerReceiver(context, powerReceiver, IntentFilter(PowerManager.ACTION_POWER_SAVE_MODE_CHANGED), ContextCompat.RECEIVER_NOT_EXPORTED)
        syncPlayback()
    }
    override fun onDetachedFromWindow() {
        releasePlayer()
        viewTreeObserver.removeOnScrollChangedListener(scrollListener)
        context.contentResolver.unregisterContentObserver(observer)
        context.unregisterReceiver(powerReceiver)
        super.onDetachedFromWindow()
    }
    override fun onWindowFocusChanged(hasWindowFocus: Boolean) { super.onWindowFocusChanged(hasWindowFocus); syncPlayback() }
    override fun onVisibilityChanged(changedView: View, visibility: Int) { super.onVisibilityChanged(changedView, visibility); syncPlayback() }
    override fun onWindowVisibilityChanged(visibility: Int) { super.onWindowVisibilityChanged(visibility); syncPlayback() }
    override fun onSurfaceTextureAvailable(surface: SurfaceTexture, width: Int, height: Int) = syncPlayback()
    override fun onSurfaceTextureSizeChanged(surface: SurfaceTexture, width: Int, height: Int) { if (prepared) player?.let { fitVideo(it.videoWidth, it.videoHeight) } }
    override fun onSurfaceTextureDestroyed(surface: SurfaceTexture): Boolean { releasePlayer(); return true }
    override fun onSurfaceTextureUpdated(surface: SurfaceTexture) = Unit
}
