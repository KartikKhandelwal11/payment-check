package com.paymentcheck.forwarder

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.provider.Telephony

/**
 * Reads incoming SMS directly (not from the Messages app's notification), so
 * the sender is the address the mobile network delivered, e.g. "VM-HDFCBK" or
 * "+919876543210". A contact saved as "HDFCBK" changes only what the Messages
 * app shows; it never changes this address.
 *
 * Only SMS that look like money credited are forwarded. OTPs and personal SMS
 * never leave the phone. The server decides whether the sender is really HDFC.
 */
class SmsReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Telephony.Sms.Intents.SMS_RECEIVED_ACTION) return
        val prefs = Prefs(context)
        if (!prefs.isConfigured) return
        val parts = Telephony.Sms.Intents.getMessagesFromIntent(intent) ?: return

        // A long SMS arrives in parts; join the parts from the same sender.
        parts.groupBy { it.originatingAddress.orEmpty() }.forEach { (sender, msgs) ->
            val body = msgs.joinToString("") { it.messageBody.orEmpty() }
            if (!CREDIT.containsMatchIn(body) || !AMOUNT.containsMatchIn(body) || OTP.containsMatchIn(body)) return@forEach
            val key = "sms-$sender-${msgs.first().timestampMillis}"
            ForwardWorker.enqueueSms(context, sender, body, key)
        }
    }

    companion object {
        private val CREDIT = Regex("""\b(credited|deposited|received)\b""", RegexOption.IGNORE_CASE)
        private val AMOUNT = Regex("""(₹|rs\.?\s*\d|inr\s*\d)""", RegexOption.IGNORE_CASE)
        private val OTP = Regex("""\b(otp|one time password)\b""", RegexOption.IGNORE_CASE)
    }
}
