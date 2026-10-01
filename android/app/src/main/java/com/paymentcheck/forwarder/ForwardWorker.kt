package com.paymentcheck.forwarder

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Sends one UPI app alert or one bank SMS to the server. If the phone is offline
 * or the server is down, WorkManager keeps it and retries, so no payment is lost.
 */
class ForwardWorker(context: Context, params: WorkerParameters) : Worker(context, params) {

    override fun doWork(): Result {
        val prefs = Prefs(applicationContext)
        if (!prefs.isConfigured) return Result.failure()

        val isSms = inputData.getString(KEY_KIND) == KIND_SMS
        val body = if (isSms) JSONObject()
            .put("sender", inputData.getString(KEY_TITLE))
            .put("body", inputData.getString(KEY_TEXT))
            .put("key", inputData.getString(KEY_NOTIF))
        else JSONObject()
            .put("package", inputData.getString(KEY_PACKAGE))
            .put("title", inputData.getString(KEY_TITLE))
            .put("text", inputData.getString(KEY_TEXT))
            .put("key", inputData.getString(KEY_NOTIF))

        val response = try {
            Api.post(prefs, if (isSms) "/api/sms" else "/api/notify", body)
        } catch (e: IOException) {
            prefs.addLog("Could not reach the server, will retry")
            return Result.retry()
        }

        return when {
            response.isSuccess -> {
                val json = response.json ?: JSONObject()
                val amount = json.optString("amount")
                val order = json.optString("order_id").takeIf { it.isNotEmpty() && it != "null" }
                val message = when (json.optString("result")) {
                    "payment" -> "🔔 UPI alert ₹$amount" + (order?.let { " (order $it, waiting for bank SMS)" } ?: "")
                    "sms" -> if (order != null) "✅ Bank SMS ₹$amount → order $order credited"
                             else "🏦 Bank SMS ₹$amount received" + (if (json.isNull("utr")) " (no UTR)" else "")
                    "sms_rejected" -> "⛔ SMS not from HDFC, ignored"
                    "duplicate" -> null
                    else -> "Not a payment, skipped"
                }
                message?.let(prefs::addLog)
                Result.success()
            }
            response.code == 401 -> {
                // Keep it: after a new phone code is entered, the retry succeeds.
                prefs.addLog("❌ Wrong phone code, get a new one from the dashboard (will retry)")
                Result.retry()
            }
            response.code >= 500 -> Result.retry()
            else -> {
                prefs.addLog("❌ Server error ${response.code}")
                Result.failure()
            }
        }
    }

    companion object {
        private const val KEY_PACKAGE = "package"
        private const val KEY_TITLE = "title"
        private const val KEY_TEXT = "text"
        private const val KEY_NOTIF = "notif_key"
        private const val KEY_KIND = "kind"
        private const val KIND_SMS = "sms"

        fun enqueue(context: Context, packageName: String, title: String, text: String, notifKey: String) {
            val request = OneTimeWorkRequestBuilder<ForwardWorker>()
                .setInputData(workDataOf(
                    KEY_PACKAGE to packageName,
                    KEY_TITLE to title.take(500),
                    KEY_TEXT to text.take(2000),
                    KEY_NOTIF to notifKey,
                ))
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 10, TimeUnit.SECONDS)
                .build()
            WorkManager.getInstance(context).enqueue(request)
        }

        fun enqueueSms(context: Context, sender: String, body: String, key: String) {
            val request = OneTimeWorkRequestBuilder<ForwardWorker>()
                .setInputData(workDataOf(
                    KEY_KIND to KIND_SMS,
                    KEY_TITLE to sender.take(40),
                    KEY_TEXT to body.take(2000),
                    KEY_NOTIF to key,
                ))
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 10, TimeUnit.SECONDS)
                .build()
            WorkManager.getInstance(context).enqueue(request)
        }
    }
}
