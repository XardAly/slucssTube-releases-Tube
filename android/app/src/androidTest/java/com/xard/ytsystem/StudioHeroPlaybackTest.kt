package com.xard.ytsystem

import android.content.ContextWrapper
import android.content.res.AssetManager
import android.os.SystemClock
import android.view.View
import android.widget.FrameLayout
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.xard.ytsystem.ui.StudioHeroView
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith

/** The synthetic clip belongs to the test APK only; it is never packaged with the app. */
@RunWith(AndroidJUnit4::class)
class StudioHeroPlaybackTest {
    @Test fun localVideoDecodesPausesLoopsAndReleasesWhenHidden() {
        com.xard.ytsystem.identity.LocalNameStore(
            androidx.test.core.app.ApplicationProvider.getApplicationContext(),
        ).save("Teste")
        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            lateinit var hero: StudioHeroView
            scenario.onActivity { activity ->
                val testAssets = InstrumentationRegistry.getInstrumentation().context.assets
                val fixtureContext = object : ContextWrapper(activity) {
                    override fun getAssets(): AssetManager = testAssets
                }
                hero = StudioHeroView(fixtureContext)
                activity.findViewById<FrameLayout>(android.R.id.content).addView(hero, FrameLayout.LayoutParams(320, 180))
                assertTrue(hero.hasVideo)
                // Pause while the decoder may still be preparing, then resume.
                hero.setMotionPaused(true)
                assertFalse(hero.isPlaying)
                hero.setMotionPaused(false)
            }
            awaitState(scenario) { hero.isPlaying && hero.getChildAt(1).alpha == 1f }
            SystemClock.sleep(1400) // Longer than the one-second fixture: looping must keep playback alive.
            scenario.onActivity {
                assertTrue("Video loops beyond its duration", hero.isPlaying)
                hero.setMotionPaused(true)
                assertFalse("Pause stops the decoder", hero.isPlaying)
                hero.setMotionPaused(false)
            }
            awaitState(scenario) { hero.isPlaying }
            scenario.onActivity {
                hero.visibility = View.GONE
                assertFalse("Hidden view releases its player", hero.isPlaying)
                hero.visibility = View.VISIBLE
            }
            awaitState(scenario) { hero.isPlaying && hero.getChildAt(1).alpha == 1f }
            scenario.onActivity {
                (hero.parent as FrameLayout).removeView(hero)
                assertFalse("Detached view releases its player", hero.isPlaying)
            }
        }
    }

    private fun awaitState(scenario: ActivityScenario<MainActivity>, ready: () -> Boolean) {
        val deadline = SystemClock.uptimeMillis() + 8000
        var success = false
        while (!success && SystemClock.uptimeMillis() < deadline) {
            scenario.onActivity { success = ready() }
            if (!success) SystemClock.sleep(50)
        }
        assertTrue("The local video reaches the expected playback state", success)
    }
}
