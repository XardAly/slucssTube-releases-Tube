package com.xard.ytsystem

import android.content.Context
import android.graphics.Bitmap
import android.os.SystemClock
import android.view.View
import android.widget.ScrollView
import androidx.recyclerview.widget.RecyclerView
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.UiController
import androidx.test.espresso.ViewAction
import androidx.test.espresso.action.ViewActions.click
import androidx.test.espresso.action.ViewActions.scrollTo
import androidx.test.espresso.action.ViewActions.closeSoftKeyboard
import androidx.test.espresso.action.ViewActions.replaceText
import androidx.test.espresso.assertion.ViewAssertions.matches
import androidx.test.espresso.matcher.ViewMatchers.*
import androidx.test.platform.app.InstrumentationRegistry
import com.google.android.material.bottomnavigation.BottomNavigationView
import com.google.android.material.chip.ChipGroup
import com.xard.ytsystem.data.DownloadJob
import com.xard.ytsystem.ui.JobAdapter
import com.xard.ytsystem.ui.StudioHeroView
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runBlocking
import org.hamcrest.Matchers.allOf
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

@RunWith(AndroidJUnit4::class)
class StudioNavigationTest {
    @Before fun resetMotionPreference() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        if (android.os.Build.VERSION.SDK_INT >= 33) InstrumentationRegistry.getInstrumentation().uiAutomation
            .grantRuntimePermission(context.packageName, android.Manifest.permission.POST_NOTIFICATIONS)
        com.xard.ytsystem.identity.LocalNameStore(ApplicationProvider.getApplicationContext()).save("Teste")
        ApplicationProvider.getApplicationContext<Context>()
            .getSharedPreferences("studio_visuals", Context.MODE_PRIVATE)
            .edit().remove("motion_paused").commit()
    }

    @Test fun downloadControlsFitWithoutScrollingAndNavigationRestoresFilter() {
        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            onView(withId(R.id.urlInput)).perform(awaitLayout()).check(matches(isCompletelyDisplayed()))
            onView(withId(R.id.pasteButton)).check(matches(isCompletelyDisplayed()))
            onView(withId(R.id.analyzeButton)).check(matches(isCompletelyDisplayed()))
            scenario.onActivity { activity ->
                val download = activity.findViewById<ScrollView>(R.id.downloadSection)
                assertEquals("Download controls are visible at the initial scroll position", 0, download.scrollY)
            }
            onView(withId(R.id.navigationGif)).perform(click())
            onView(withId(R.id.gifSection)).check(matches(isDisplayed()))
            scenario.onActivity {
                assertEquals(R.id.navigationGif, it.findViewById<BottomNavigationView>(R.id.bottomNavigation).selectedItemId)
            }
            onView(withId(R.id.navigationQueue)).perform(click())
            onView(withId(R.id.emptyQueueAction)).perform(awaitLayout(), scrollTo())
                .check(matches(isDisplayed()))
            scenario.onActivity {
                val action = it.findViewById<android.view.View>(R.id.emptyQueueAction)
                assertTrue("Empty queue action keeps its touch target", action.height >= 48 * it.resources.displayMetrics.density)
            }
            onView(withId(R.id.filterActive)).perform(awaitLayout(), click())
            scenario.recreate()
            onView(withId(R.id.queueSection)).check(matches(isDisplayed()))
            scenario.onActivity {
                assertEquals(R.id.filterActive, it.findViewById<ChipGroup>(R.id.queueFilters).checkedChipId)
                assertEquals(R.id.navigationQueue, it.findViewById<BottomNavigationView>(R.id.bottomNavigation).selectedItemId)
            }
        }
    }

    @Test fun allToolScreensRemainReachableWithoutArtwork() {
        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            onView(withId(R.id.urlInput)).perform(awaitLayout()).check(matches(isCompletelyDisplayed()))
            saveScreen("professional-home.png")
            val tabs = listOf(
                Triple(R.id.navigationGif, R.id.gifSection, "professional-gif.png"),
                Triple(R.id.navigationCompatibility, R.id.compatibilitySection, "professional-convert.png"),
                Triple(R.id.navigationUpscale, R.id.upscaleSection, "professional-upscale.png"),
                Triple(R.id.navigationQueue, R.id.queueSection, "professional-queue.png"),
            )
            for ((tab, section, filename) in tabs) {
                onView(withId(tab)).perform(click())
                onView(withId(section)).perform(awaitLayout()).check(matches(isDisplayed()))
                saveScreen(filename)
            }
            onView(withId(R.id.navigationDownload)).perform(click())
            scenario.recreate()
            onView(withId(R.id.downloadSection)).check(matches(isDisplayed()))
        }
    }

    private fun saveScreen(name: String) {
        SystemClock.sleep(300) // Capture after the native selection ripple and window transition settle.
        val context = ApplicationProvider.getApplicationContext<Context>()
        val bitmap = InstrumentationRegistry.getInstrumentation().uiAutomation.takeScreenshot() ?: return
        try {
            File(context.getExternalFilesDir(null), name).outputStream().use {
                assertTrue(bitmap.compress(Bitmap.CompressFormat.PNG, 100, it))
            }
        } finally { bitmap.recycle() }
    }

    @Test fun keyboardKeepsTheLinkReachableAndRejectsInvalidInput() {
        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            onView(withId(R.id.urlInput)).perform(awaitLayout(), click(), replaceText("invalid-link"))
            scenario.onActivity { activity ->
                val input = activity.findViewById<View>(R.id.urlInput)
                input.requestFocus()
                activity.getSystemService(android.view.inputmethod.InputMethodManager::class.java)
                    .showSoftInput(input, android.view.inputmethod.InputMethodManager.SHOW_IMPLICIT)
            }
            onView(withId(R.id.urlInput)).perform(object : ViewAction {
                override fun getDescription() = "wait for the software keyboard insets"
                override fun getConstraints() = isAssignableFrom(View::class.java)
                override fun perform(controller: UiController, view: View) {
                    val deadline = SystemClock.uptimeMillis() + 5000
                    fun shown() = androidx.core.view.ViewCompat.getRootWindowInsets(view)
                        ?.isVisible(androidx.core.view.WindowInsetsCompat.Type.ime()) == true
                    while (!shown() && SystemClock.uptimeMillis() < deadline) controller.loopMainThreadForAtLeast(50)
                    assertTrue("Software keyboard is visible", shown())
                }
            })
            onView(withId(R.id.urlInput)).check(matches(isDisplayed()))
            onView(withId(R.id.bottomNavigation)).check(matches(withEffectiveVisibility(Visibility.GONE)))
            saveScreen("professional-keyboard.png")
            onView(withId(R.id.urlInput)).perform(closeSoftKeyboard())
            onView(withId(R.id.analyzeButton)).perform(scrollTo(), click())
            scenario.onActivity { activity ->
                assertTrue("Invalid input has a local field error", !activity.findViewById<android.widget.EditText>(R.id.urlInput).error.isNullOrBlank())
                assertEquals(View.GONE, activity.findViewById<View>(R.id.analyzeProgress).visibility)
            }
        }
    }

    @Test fun populatedQueueFiltersCompletedAndFailedJobs() {
        val app = ApplicationProvider.getApplicationContext<SlucssApplication>()
        val suffix = SystemClock.uptimeMillis()
        val completedId = "qa_antislop_completed_$suffix"
        val failedId = "qa_antislop_failed_$suffix"
        val fixtureIds = setOf(completedId, failedId)
        try {
            ActivityScenario.launch(MainActivity::class.java).use {
                // Insert only terminal jobs after startup; these fixtures never enter the worker queue.
                onView(withId(R.id.urlInput)).perform(awaitLayout()).check(matches(isDisplayed()))
                val now = System.currentTimeMillis()
                runBlocking(Dispatchers.IO) {
                    app.database.jobs().upsert(DownloadJob(
                        id = completedId,
                        type = DownloadJob.TYPE_GIF,
                        title = "Amostra QA · abertura em GIF",
                        status = DownloadJob.STATUS_COMPLETED,
                        stage = "completed",
                        progress = 100f,
                        outputName = "amostra-abertura.gif",
                        createdAt = now + 1,
                        updatedAt = now + 1,
                    ))
                    app.database.jobs().upsert(DownloadJob(
                        id = failedId,
                        type = DownloadJob.TYPE_COMPATIBILITY,
                        title = "Amostra QA · conversão de vídeo",
                        status = DownloadJob.STATUS_FAILED,
                        stage = "failed",
                        error = "Arquivo de exemplo indisponível.",
                        errorDetail = "Exemplo de QA: a origem de teste não contém mídia; nenhum processo foi iniciado.",
                        createdAt = now,
                        updatedAt = now,
                    ))
                }
                onView(withId(R.id.navigationQueue)).perform(click())
                onView(withId(R.id.filterAll)).perform(awaitLayout(), click())
                onView(withId(R.id.jobsList)).perform(awaitQueueFixtures(fixtureIds, fixtureIds))
                saveQueueScreenshot(app)

                onView(withId(R.id.filterCompleted)).perform(click())
                onView(withId(R.id.jobsList)).perform(awaitQueueFixtures(
                    fixtureIds, setOf(completedId), DownloadJob.STATUS_COMPLETED,
                ))
                onView(allOf(withText("Amostra QA · abertura em GIF"), isDescendantOfA(withId(R.id.jobsList)))).check(matches(isDisplayed()))

                onView(withId(R.id.filterFailed)).perform(click())
                onView(withId(R.id.jobsList)).perform(awaitQueueFixtures(
                    fixtureIds, setOf(failedId), DownloadJob.STATUS_FAILED,
                ))
                onView(allOf(withText("Amostra QA · conversão de vídeo"), isDescendantOfA(withId(R.id.jobsList)))).check(matches(isDisplayed()))
                onView(withId(R.id.jobsList)).perform(object : ViewAction {
                    override fun getDescription() = "reveal the failed job action in a scrollable list"
                    override fun getConstraints() = isAssignableFrom(RecyclerView::class.java)
                    override fun perform(controller: UiController, view: View) {
                        val action = view.findViewById<View>(R.id.jobErrorButton)
                        action.requestRectangleOnScreen(android.graphics.Rect(0, 0, action.width, action.height), true)
                        controller.loopMainThreadForAtLeast(250)
                    }
                })
                onView(allOf(withId(R.id.jobErrorButton), isDisplayed(), isDescendantOfA(allOf(
                    withParent(withId(R.id.jobsList)), hasDescendant(withText("Amostra QA · conversão de vídeo")),
                ))))
                    .check(matches(withText("Ver erro")))
            }
        } finally {
            runBlocking(Dispatchers.IO) {
                app.database.openHelper.writableDatabase.execSQL(
                    "DELETE FROM download_jobs WHERE id IN (?, ?)",
                    arrayOf<Any>(completedId, failedId),
                )
            }
        }
    }

    private fun saveQueueScreenshot(context: Context) {
        val directory = context.getExternalFilesDir(null) ?: return
        val bitmap = InstrumentationRegistry.getInstrumentation().uiAutomation.takeScreenshot() ?: return
        try {
            File(directory, "antislop-queue.png").outputStream().use {
                assertTrue("Queue screenshot is written", bitmap.compress(Bitmap.CompressFormat.PNG, 100, it))
            }
        } finally {
            bitmap.recycle()
        }
    }

    private fun awaitQueueFixtures(
        fixtureIds: Set<String>, expectedIds: Set<String>, expectedStatus: String? = null,
    ) = object : ViewAction {
        override fun getDescription() = "wait for queue filtering and row layout to finish"
        override fun getConstraints() = isAssignableFrom(RecyclerView::class.java)
        override fun perform(controller: UiController, view: View) {
            val list = view as RecyclerView
            val adapter = list.adapter as JobAdapter
            fun ready(): Boolean {
                val rows = adapter.currentList
                return rows.filter { it.id in fixtureIds }.map { it.id }.toSet() == expectedIds &&
                    (expectedStatus == null || rows.all { it.status == expectedStatus }) &&
                    !list.isLayoutRequested && !list.isComputingLayout && list.childCount > 0
            }
            val deadline = SystemClock.uptimeMillis() + 4000
            while (!ready() && SystemClock.uptimeMillis() < deadline) controller.loopMainThreadForAtLeast(16)
            assertTrue("The selected filter shows only its expected fixture and status", ready())
            controller.loopMainThreadForAtLeast(300) // Settle RecyclerView transitions before pixel capture.
        }
    }

    private fun awaitLayout() = object : ViewAction {
        override fun getDescription() = "wait for the newly visible control to complete layout"
        override fun getConstraints() = isAssignableFrom(View::class.java)
        override fun perform(controller: UiController, view: View) {
            val deadline = SystemClock.uptimeMillis() + 3000
            while ((view.width == 0 || view.isLayoutRequested) && SystemClock.uptimeMillis() < deadline) {
                controller.loopMainThreadForAtLeast(16)
            }
            assertTrue("Control must be laid out", view.width > 0 && !view.isLayoutRequested)
        }
    }
}
