package com.paymentcheck.forwarder

import android.content.Context
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class Prefs(context: Context) {
    private val sp = context.applicationContext.getSharedPreferences("forwarder", Context.MODE_PRIVATE)

    var serverUrl: String
        get() = sp.getString("server_url", "") ?: ""
        set(value) = sp.edit().putString("server_url", value.trim().trimEnd('/')).apply()

    var deviceCode: String
        get() = sp.getString("device_code", "") ?: ""
        set(value) = sp.edit().putString("device_code", value.trim()).apply()

    val isConfigured: Boolean
        get() = serverUrl.isNotEmpty() && deviceCode.isNotEmpty()

    /** Last few things the app did, shown on the main screen. */
    val log: List<String>
        get() = (sp.getString("log", "") ?: "").split('\n').filter { it.isNotBlank() }

    @Synchronized
    fun addLog(message: String) {
        val time = SimpleDateFormat("dd MMM HH:mm", Locale.getDefault()).format(Date())
        val lines = (listOf("$time  $message") + log).take(MAX_LOG_LINES)
        sp.edit().putString("log", lines.joinToString("\n")).apply()
    }

    companion object {
        private const val MAX_LOG_LINES = 15
    }
}
