package com.paymentcheck.forwarder

import android.app.Notification
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification

/**
 * Receives every notification on the phone, but forwards only those from the
 * payment apps below that mention an amount. Everything else (WhatsApp,
 * OTPs, other apps) is dropped here and never leaves the phone. Bank SMS are
 * read by SmsReceiver instead, because a notification shows the contact name,
 * which anyone can set to "HDFCBK".
 */
class PaymentListenerService : NotificationListenerService() {

    override fun onListenerConnected() {
        HeartbeatWorker.schedule(this)
    }

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        if (sbn.packageName !in PAYMENT_APPS) return
        val notification = sbn.notification ?: return
        if (notification.flags and Notification.FLAG_GROUP_SUMMARY != 0) return

        val extras = notification.extras
        val title = extras.getCharSequence(Notification.EXTRA_TITLE)?.toString().orEmpty()
        val text = (extras.getCharSequence(Notification.EXTRA_BIG_TEXT)
            ?: extras.getCharSequence(Notification.EXTRA_TEXT))?.toString().orEmpty()
        if (!AMOUNT.containsMatchIn("$title $text")) return

        val prefs = Prefs(this)
        if (!prefs.isConfigured) return
        ForwardWorker.enqueue(this, sbn.packageName, title, text, sbn.key)
    }

    companion object {
        /** Apps whose notifications are forwarded. Add a package name here to support another app. */
        val PAYMENT_APPS = setOf(
            "com.phonepe.app",                          // PhonePe
            "net.one97.paytm",                          // Paytm
            "com.paytm.business",                       // Paytm for Business
            "com.google.android.apps.nbu.paisa.user",   // Google Pay
        )

        private val AMOUNT = Regex("""(₹|rs\.?\s*\d|inr\s*\d)""", RegexOption.IGNORE_CASE)
    }
}
