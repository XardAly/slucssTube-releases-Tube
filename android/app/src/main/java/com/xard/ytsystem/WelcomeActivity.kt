package com.xard.ytsystem

import android.content.Intent
import android.os.Bundle
import android.view.inputmethod.EditorInfo
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.updatePadding
import androidx.core.widget.doAfterTextChanged
import com.xard.ytsystem.databinding.ActivityWelcomeBinding
import com.xard.ytsystem.identity.LocalNameStore
import com.xard.ytsystem.ui.AppMotion

class WelcomeActivity : AppCompatActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val store = LocalNameStore(this)
        val editing = intent.getBooleanExtra("edit_name", false)
        if (!editing && store.name.isNotBlank()) {
            openApp()
            return
        }
        val binding = ActivityWelcomeBinding.inflate(layoutInflater)
        setContentView(binding.root)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        ViewCompat.setOnApplyWindowInsetsListener(binding.root) { view, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            val ime = insets.getInsets(WindowInsetsCompat.Type.ime())
            view.updatePadding(top = bars.top, bottom = maxOf(bars.bottom, ime.bottom), left = bars.left, right = bars.right)
            if (insets.isVisible(WindowInsetsCompat.Type.ime()) && binding.nameInput.hasFocus()) {
                view.post {
                    binding.nameLayout.requestRectangleOnScreen(
                        android.graphics.Rect(0, 0, binding.nameLayout.width, binding.nameLayout.height), true,
                    )
                }
            }
            insets
        }
        if (editing) {
            binding.welcomeEyebrow.text = "Alterar nome"
            binding.continueButton.text = "Salvar nome"
            if (savedInstanceState == null) binding.nameInput.setText(store.name)
        }
        binding.nameInput.doAfterTextChanged { binding.nameLayout.error = null }
        fun continueWithName() {
            val name = LocalNameStore.normalize(binding.nameInput.text?.toString().orEmpty())
            if (name.isEmpty()) {
                binding.nameLayout.error = "Digite como podemos te chamar."
                binding.nameInput.requestFocus()
                return
            }
            store.save(name)
            if (editing) finish() else openApp()
        }
        binding.continueButton.setOnClickListener { continueWithName() }
        binding.nameInput.setOnEditorActionListener { _, action, _ ->
            if (action == EditorInfo.IME_ACTION_DONE) { continueWithName(); true } else false
        }
        if (savedInstanceState == null) AppMotion.enter(binding.root.getChildAt(0))
    }

    private fun openApp() {
        startActivity(Intent(this, MainActivity::class.java))
        finish()
    }
}
