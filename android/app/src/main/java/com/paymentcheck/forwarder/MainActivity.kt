package com.paymentcheck.forwarder

import android.Manifest
import android.annotation.SuppressLint
import android.content.pm.PackageManager
import android.app.Activity
import android.content.Intent
import android.graphics.Typeface
import android.net.Uri
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.text.InputType
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import java.io.IOException

class MainActivity : Activity() {

    private lateinit var prefs: Prefs
    private lateinit var accessStatus: TextView
    private lateinit var batteryStatus: TextView
    private lateinit var smsStatus: TextView
    private lateinit var serverStatus: TextView
    private lateinit var serverInput: EditText
    private lateinit var codeInput: EditText
    private lateinit var logView: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        setContentView(buildLayout())
        serverInput.setText(prefs.serverUrl)
        codeInput.setText(prefs.deviceCode)
        if (prefs.isConfigured) HeartbeatWorker.schedule(this)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        refresh()
    }

    override fun onResume() {
        super.onResume()
        refresh()
    }

    private fun refresh() {
        accessStatus.text = if (hasNotificationAccess()) "✅ Notification access granted"
        else "❌ No notification access (tap the button below)"

        smsStatus.text = if (checkSelfPermission(Manifest.permission.RECEIVE_SMS) == PackageManager.PERMISSION_GRANTED)
            "✅ Bank SMS are read (only credit messages are sent)"
        else "❌ SMS permission missing (tap the button below)"

        val pm = getSystemService(POWER_SERVICE) as PowerManager
        batteryStatus.text = if (pm.isIgnoringBatteryOptimizations(packageName)) "✅ Battery restriction removed"
        else "⚠️ Battery saver may stop the app (tap the button below)"

        val log = prefs.log
        logView.text = if (log.isEmpty()) "Nothing yet" else log.joinToString("\n")
    }

    private fun hasNotificationAccess(): Boolean {
        val enabled = Settings.Secure.getString(contentResolver, "enabled_notification_listeners") ?: return false
        return enabled.split(':').any { it.startsWith("$packageName/") }
    }

    private fun saveAndTest() {
        val url = serverInput.text.toString().trim()
        val code = codeInput.text.toString().trim()
        if (!url.startsWith("http://") && !url.startsWith("https://")) {
            serverStatus.text = "❌ Server address must start with http:// or https://"
            return
        }
        if (code.isEmpty()) {
            serverStatus.text = "❌ Enter the phone code (from the dashboard)"
            return
        }
        prefs.serverUrl = url
        prefs.deviceCode = code
        serverStatus.text = "Checking…"

        Thread {
            val message = try {
                val response = Api.post(prefs, "/api/heartbeat")
                when {
                    response.isSuccess -> {
                        val json = response.json
                        HeartbeatWorker.schedule(this)
                        "✅ Connected: ${json?.optString("member")} (${json?.optString("name")})"
                    }
                    response.code == 401 -> "❌ Wrong code"
                    else -> "❌ Server error ${response.code}"
                }
            } catch (e: IOException) {
                "❌ Could not reach the server. Check the address and internet."
            }
            runOnUiThread {
                serverStatus.text = message
                prefs.addLog(message)
                refresh()
            }
        }.start()
    }

    @SuppressLint("BatteryLife")
    private fun askBatteryExemption() {
        val intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName"))
        runCatching { startActivity(intent) }
            .onFailure { startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)) }
    }

    // ---------- layout ----------

    private fun dp(value: Int) = (value * resources.displayMetrics.density).toInt()

    private fun buildLayout(): View {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(20), dp(20), dp(32))
        }

        fun heading(text: String) = TextView(this).apply {
            this.text = text
            textSize = 17f
            setTypeface(typeface, Typeface.BOLD)
            setPadding(0, dp(20), 0, dp(6))
        }.also(root::addView)

        fun line(): TextView = TextView(this).apply {
            textSize = 15f
            setPadding(0, dp(4), 0, dp(4))
        }.also(root::addView)

        fun button(text: String, onClick: () -> Unit) = Button(this).apply {
            this.text = text
            isAllCaps = false
            setOnClickListener { onClick() }
        }.also(root::addView)

        TextView(this).apply {
            text = "Sends PhonePe / Paytm / GPay payment notifications to your Payment Check server. No other notification leaves the phone."
            textSize = 14f
        }.also(root::addView)

        heading("1. Connect to server")
        serverInput = EditText(this).apply {
            hint = "Server address, e.g. https://pay.example.com"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            isSingleLine = true
        }.also(root::addView)
        codeInput = EditText(this).apply {
            hint = "Phone code (from the dashboard)"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
            isSingleLine = true
        }.also(root::addView)
        button("Save and test") { saveAndTest() }
        serverStatus = line()

        heading("2. Allow notification access")
        accessStatus = line()
        button("Open notification access settings") {
            startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
        }

        heading("3. Allow reading bank SMS")
        smsStatus = line()
        button("Allow SMS") { requestPermissions(arrayOf(Manifest.permission.RECEIVE_SMS), 1) }

        heading("4. Remove battery restriction")
        batteryStatus = line()
        button("Remove battery restriction") { askBatteryExemption() }

        heading("Recent activity")
        logView = TextView(this).apply {
            textSize = 13f
            typeface = Typeface.MONOSPACE
        }.also(root::addView)

        return ScrollView(this).apply { addView(root) }
    }
}
