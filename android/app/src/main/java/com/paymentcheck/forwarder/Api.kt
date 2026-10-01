package com.paymentcheck.forwarder

import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

class ApiResponse(val code: Int, val body: String) {
    val isSuccess get() = code in 200..299
    val json: JSONObject? get() = runCatching { JSONObject(body) }.getOrNull()
}

object Api {
    /** POSTs [body] to the server. Throws IOException when the server can't be reached. */
    @Throws(IOException::class)
    fun post(prefs: Prefs, path: String, body: JSONObject = JSONObject()): ApiResponse {
        val conn = URL(prefs.serverUrl + path).openConnection() as HttpURLConnection
        try {
            conn.requestMethod = "POST"
            conn.connectTimeout = 15_000
            conn.readTimeout = 15_000
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json; charset=utf-8")
            conn.setRequestProperty("Authorization", "Bearer ${prefs.deviceCode}")
            conn.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }

            val code = conn.responseCode
            val stream = if (code in 200..299) conn.inputStream else conn.errorStream
            val text = stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() } ?: ""
            return ApiResponse(code, text)
        } finally {
            conn.disconnect()
        }
    }
}
