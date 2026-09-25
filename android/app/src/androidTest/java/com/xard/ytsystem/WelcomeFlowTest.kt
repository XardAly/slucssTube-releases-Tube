package com.xard.ytsystem

import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.os.SystemClock
import android.view.View
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.ViewAction
import androidx.test.espresso.UiController
import androidx.test.espresso.action.ViewActions.*
import androidx.test.espresso.assertion.ViewAssertions.matches
import androidx.test.espresso.matcher.ViewMatchers.*
import androidx.test.platform.app.InstrumentationRegistry
import com.xard.ytsystem.identity.LocalNameStore
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

@RunWith(AndroidJUnit4::class)
class WelcomeFlowTest {
    @org.junit.Before fun grantNotifications() {
        if (android.os.Build.VERSION.SDK_INT >= 33) InstrumentationRegistry.getInstrumentation().uiAutomation
            .grantRuntimePermission(ApplicationProvider.getApplicationContext<Context>().packageName, android.Manifest.permission.POST_NOTIFICATIONS)
    }

    private val context: Context get() = ApplicationProvider.getApplicationContext()

    @Test fun nameIsRequiredPersistsLocallyAndCanBeEditedWithoutAnAccount() {
        context.getSharedPreferences("local_name", Context.MODE_PRIVATE).edit().clear().commit()
        ActivityScenario.launch(WelcomeActivity::class.java).use { scenario ->
            onView(withId(R.id.continueButton)).perform(scrollTo()).check(matches(isDisplayed()))
            screen("welcome.png")
            onView(withId(R.id.continueButton)).perform(click())
            assertEquals("", LocalNameStore(context).name)
            onView(withId(R.id.nameInput)).perform(scrollTo(), replaceText("João"))
            scenario.recreate()
            onView(withId(R.id.nameInput)).check(matches(withText("João")))
            onView(withId(R.id.nameInput)).perform(closeSoftKeyboard())
            onView(withId(R.id.continueButton)).perform(scrollTo(), click())
            assertEquals("João", LocalNameStore(context).name)
            onView(withId(R.id.downloadSection)).check(matches(isDisplayed()))
            onView(withId(R.id.settingsButton)).perform(click())
            onView(withText("Alterar nome")).perform(click())
            onView(withId(R.id.nameInput)).check(matches(withText("João")))
            onView(withId(R.id.nameInput)).perform(replaceText("Maria"), closeSoftKeyboard())
            onView(withId(R.id.continueButton)).perform(scrollTo(), click())
            assertEquals("Maria", LocalNameStore(context).name)
            onView(withId(R.id.downloadSection)).check(matches(isDisplayed()))
        }
        ActivityScenario.launch(WelcomeActivity::class.java).use {
            onView(withId(R.id.downloadSection)).check(matches(isDisplayed()))
        }
    }

    @Test fun keyboardAndLargeTextKeepNameAndContinueReachable() {
        LocalNameStore(context).save("Ana")
        val intent = Intent(context, WelcomeActivity::class.java).putExtra("edit_name", true)
        ActivityScenario.launch<WelcomeActivity>(intent).use { scenario ->
            onView(withId(R.id.nameInput)).perform(scrollTo(), click(), replaceText("Ana Maria"))
            scenario.onActivity { activity ->
                val input = activity.findViewById<View>(R.id.nameInput)
                input.requestFocus()
                activity.getSystemService(android.view.inputmethod.InputMethodManager::class.java)
                    .showSoftInput(input, android.view.inputmethod.InputMethodManager.SHOW_IMPLICIT)
            }
            onView(withId(R.id.nameInput)).perform(object : ViewAction {
                override fun getDescription() = "wait for name field keyboard"
                override fun getConstraints() = isAssignableFrom(View::class.java)
                override fun perform(controller: UiController, view: View) {
                    val deadline = SystemClock.uptimeMillis() + 5000
                    fun shown() = androidx.core.view.ViewCompat.getRootWindowInsets(view)
                        ?.isVisible(androidx.core.view.WindowInsetsCompat.Type.ime()) == true
                    while (!shown() && SystemClock.uptimeMillis() < deadline) controller.loopMainThreadForAtLeast(50)
                    assertTrue(shown())
                }

            }).check(matches(isCompletelyDisplayed()))
            screen("welcome-keyboard.png")
            onView(withId(R.id.nameInput)).perform(closeSoftKeyboard())
            onView(withId(R.id.continueButton)).perform(scrollTo()).check(matches(isCompletelyDisplayed()))
        }
    }

    private fun screen(name: String) {
        val bitmap = InstrumentationRegistry.getInstrumentation().uiAutomation.takeScreenshot() ?: error("No screenshot")
        File(context.getExternalFilesDir(null), name).outputStream().use { bitmap.compress(Bitmap.CompressFormat.PNG, 100, it) }
        bitmap.recycle()
    }
}
