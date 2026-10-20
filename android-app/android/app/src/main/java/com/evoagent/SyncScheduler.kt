package com.evoagent

import android.content.Context
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import com.facebook.react.bridge.Arguments
import com.facebook.react.bridge.WritableMap
import java.util.concurrent.TimeUnit

/**
 * 周期同步调度：联网 + 电量不低，最短 15 分钟（WorkManager 下限）。
 * 不启动前台轮询，也不改变用户手动同步；系统可在应用退到后台后继续唤醒。
 */
object SyncScheduler {
    private const val UNIQUE_PERIODIC = "evoagent-background-sync"
    private const val UNIQUE_ONCE = "evoagent-background-sync-now"
    private const val PREFS = "evoagent_background_sync"
    private const val KEY_ENABLED = "enabled"
    private const val KEY_INTERVAL = "interval_minutes"
    private const val KEY_LAST_ATTEMPT = "last_attempt_at"
    private const val MIN_INTERVAL = 15
    private const val MAX_INTERVAL = 24 * 60
    private const val DEFAULT_INTERVAL = 30

    fun schedule(context: Context, intervalMinutes: Int, runNow: Boolean) {
        val minutes = intervalMinutes.coerceIn(MIN_INTERVAL, MAX_INTERVAL)
        prefs(context).edit()
            .putBoolean(KEY_ENABLED, true)
            .putInt(KEY_INTERVAL, minutes)
            .apply()
        val request = PeriodicWorkRequestBuilder<SyncWorker>(
            minutes.toLong(),
            TimeUnit.MINUTES,
        ).setConstraints(constraints()).build()
        WorkManager.getInstance(context).enqueueUniquePeriodicWork(
            UNIQUE_PERIODIC,
            ExistingPeriodicWorkPolicy.UPDATE,
            request,
        )
        if (runNow) enqueueOnce(context)
    }

    fun enqueueOnce(context: Context) {
        val request = OneTimeWorkRequestBuilder<SyncWorker>()
            .setConstraints(constraints())
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork(
            UNIQUE_ONCE,
            ExistingWorkPolicy.REPLACE,
            request,
        )
    }

    fun cancel(context: Context) {
        val manager = WorkManager.getInstance(context)
        manager.cancelUniqueWork(UNIQUE_PERIODIC)
        manager.cancelUniqueWork(UNIQUE_ONCE)
        prefs(context).edit().putBoolean(KEY_ENABLED, false).apply()
    }

    fun status(context: Context): WritableMap {
        val prefs = prefs(context)
        return Arguments.createMap().apply {
            putBoolean("enabled", prefs.getBoolean(KEY_ENABLED, false))
            putInt("interval_minutes", prefs.getInt(KEY_INTERVAL, DEFAULT_INTERVAL))
            putDouble(
                "last_attempt_at",
                prefs.getLong(KEY_LAST_ATTEMPT, 0L).toDouble(),
            )
        }
    }

    fun recordAttempt(context: Context) {
        prefs(context).edit()
            .putLong(KEY_LAST_ATTEMPT, System.currentTimeMillis())
            .apply()
    }

    private fun prefs(context: Context) =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    private fun constraints(): Constraints = Constraints.Builder()
        .setRequiredNetworkType(NetworkType.CONNECTED)
        .setRequiresBatteryNotLow(true)
        .build()
}
