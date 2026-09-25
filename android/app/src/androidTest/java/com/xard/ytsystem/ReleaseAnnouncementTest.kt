package com.xard.ytsystem

import android.Manifest
import android.content.Context
import android.graphics.Bitmap
import android.os.Build
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.Espresso.pressBack
import androidx.test.espresso.action.ViewActions.click
import androidx.test.espresso.assertion.ViewAssertions.matches
import androidx.test.espresso.matcher.ViewMatchers.*
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.xard.ytsystem.api.AndroidManifest
import com.xard.ytsystem.identity.LocalNameStore
import com.xard.ytsystem.update.ReleaseAnnouncement
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

@RunWith(AndroidJUnit4::class)
class ReleaseAnnouncementTest {
    private val context = ApplicationProvider.getApplicationContext<Context>()

    @Before fun prepare() {
        LocalNameStore(context).save("Teste")
        if (Build.VERSION.SDK_INT >= 33) InstrumentationRegistry.getInstrumentation()
            .uiAutomation.grantRuntimePermission(context.packageName, Manifest.permission.POST_NOTIFICATIONS)
        context.getSharedPreferences("release_announcement", Context.MODE_PRIVATE).edit().clear().commit()
    }

    private fun manifest() = AndroidManifest(
        "1.3.2", BuildConfig.VERSION_CODE + 1, 1, "https://example.invalid/app.apk", "a".repeat(64),
        listOf("Imagem de anúncio dentro do aplicativo.", "Downloads com nova tentativa e compartilhamento.",
            "Melhorias de estabilidade e tratamento de erros."),
        "local", false, true, 20, "test", "test", "test", "test-next", "2026-09-25T00:00:00Z", "recommended",
    )

    private fun screenshot(name: String) {
        android.os.SystemClock.sleep(450) // Let the native dialog entrance finish before capture.
        InstrumentationRegistry.getInstrumentation().waitForIdleSync()
        val bitmap = InstrumentationRegistry.getInstrumentation().uiAutomation.takeScreenshot()
        File(context.getExternalFilesDir(null), name).outputStream().use {
            bitmap.compress(Bitmap.CompressFormat.PNG, 100, it)
        }
        bitmap.recycle()
    }

    @Test fun onlineArtworkAppearsOnceAndPersistsAcrossActivityRecreation() {
        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            scenario.onActivity { assertTrue(ReleaseAnnouncement.showOnlineIfNeeded(it)) }
            onView(withId(R.id.releaseBanner)).check(matches(isCompletelyDisplayed()))
            onView(withText("Continuar")).check(matches(isCompletelyDisplayed()))
            screenshot("release-banner-online.png")
            onView(withText("Continuar")).perform(click())
            scenario.recreate()
            scenario.onActivity { assertFalse(ReleaseAnnouncement.showOnlineIfNeeded(it)) }
        }
    }

    @Test fun optionalUpdateShowsWholeBannerAndDownloadAction() {
        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            var downloaded = false
            scenario.onActivity { ReleaseAnnouncement.showUpdate(it, manifest(), false) { downloaded = true } }
            onView(withId(R.id.releaseBanner)).check(matches(isCompletelyDisplayed()))
            onView(withText("Depois")).check(matches(isCompletelyDisplayed()))
            onView(withText("Baixar")).check(matches(isCompletelyDisplayed()))
            screenshot("release-banner-update.png")
            onView(withText("Baixar")).perform(click())
            scenario.onActivity { assertTrue(downloaded) }
        }
    }

    @Test fun mandatoryUpdateCannotBeDismissedWithBack() {
        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            scenario.onActivity { ReleaseAnnouncement.showUpdate(it, manifest(), true) {} }
            pressBack()
            onView(withText("Atualização necessária")).check(matches(isDisplayed()))
            onView(withText("Baixar")).check(matches(isCompletelyDisplayed()))
            screenshot("release-banner-required.png")
            onView(withText("Baixar")).perform(click())
        }
    }
}
