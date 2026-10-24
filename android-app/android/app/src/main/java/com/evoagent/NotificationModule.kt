package com.evoagent

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import com.facebook.react.bridge.Promise
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.react.bridge.ReactContextBaseJavaModule
import com.facebook.react.bridge.ReactMethod

/**
 * 任务完成提醒（PRD 64）。通知是尽力而为：权限缺失或系统拒绝时返回 false，
 * 不抛异常、不影响后台同步。
 */
class NotificationModule(
    private val reactContext: ReactApplicationContext,
) : ReactContextBaseJavaModule(reactContext) {

    companion object {
        private const val CHANNEL_ID = "evoagent_tasks"
        private const val CHANNEL_NAME = "任务通知"
        private const val MAX_TITLE_CHARS = 120
        private const val MAX_BODY_CHARS = 1000
    }

    override fun getName(): String = "EvoAgentNotifications"

    @ReactMethod
    fun notifyTaskUpdate(id: Double, title: String, body: String, promise: Promise) {
        try {
            if (!NotificationManagerCompat.from(reactContext).areNotificationsEnabled()) {
                promise.resolve(false)
                return
            }
            val manager =
                reactContext.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            ensureChannel(manager)
            val builder = NotificationCompat.Builder(reactContext, CHANNEL_ID)
                .setSmallIcon(notificationIcon())
                .setContentTitle(title.take(MAX_TITLE_CHARS))
                .setContentText(body.take(MAX_TITLE_CHARS))
                .setStyle(NotificationCompat.BigTextStyle().bigText(body.take(MAX_BODY_CHARS)))
                .setAutoCancel(true)
                .setOnlyAlertOnce(true)
            pendingIntent()?.let { builder.setContentIntent(it) }
            manager.notify(id.toLong().toInt() and 0x7fffffff, builder.build())
            promise.resolve(true)
        } catch (error: Exception) {
            // 通知失败不阻断同步；调用方只把它当尽力而为
            promise.resolve(false)
        }
    }

    private fun ensureChannel(manager: NotificationManager) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        if (manager.getNotificationChannel(CHANNEL_ID) != null) return
        val channel = NotificationChannel(
            CHANNEL_ID,
            CHANNEL_NAME,
            NotificationManager.IMPORTANCE_DEFAULT,
        ).apply {
            description = "任务完成或失败提醒"
        }
        manager.createNotificationChannel(channel)
    }

    private fun notificationIcon(): Int =
        reactContext.applicationInfo.icon.takeIf { it != 0 }
            ?: android.R.drawable.stat_notify_more

    private fun pendingIntent(): PendingIntent? {
        val intent = reactContext.packageManager
            .getLaunchIntentForPackage(reactContext.packageName) ?: return null
        return PendingIntent.getActivity(
            reactContext,
            0,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
    }
}
