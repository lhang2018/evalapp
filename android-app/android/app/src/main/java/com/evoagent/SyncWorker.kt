package com.evoagent

import android.content.Context
import android.content.pm.ServiceInfo
import android.os.Build
import androidx.work.ForegroundInfo
import androidx.work.Worker
import androidx.work.WorkerParameters
import java.util.concurrent.TimeUnit

/** 在 WorkManager 任务窗口内直接跑无界面 JS；真正的同步逻辑在 TS 离线层。 */
class SyncWorker(
    context: Context,
    params: WorkerParameters,
) : Worker(context, params) {

    override fun doWork(): Result {
        SyncScheduler.recordAttempt(applicationContext)
        // 通知只是可见性增强；Android 12+ 从后台启动 FGS 可能被拒，
        // 拿不到就继续跑，同步不依赖前台服务
        try {
            setForegroundAsync(foregroundInfo()).get(FOREGROUND_TIMEOUT_SECONDS, TimeUnit.SECONDS)
        } catch (error: Exception) {
            // ignore: run without foreground notification
        }
        return if (HeadlessSyncRunner.run(applicationContext, TASK_TIMEOUT_MS)) {
            Result.success()
        } else {
            Result.retry()
        }
    }

    private fun foregroundInfo(): ForegroundInfo =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            ForegroundInfo(
                NOTIFICATION_ID,
                SyncNotifications.build(applicationContext),
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC,
            )
        } else {
            ForegroundInfo(NOTIFICATION_ID, SyncNotifications.build(applicationContext))
        }

    companion object {
        private const val NOTIFICATION_ID = 4711
        private const val FOREGROUND_TIMEOUT_SECONDS = 5L
        private const val TASK_TIMEOUT_MS = 60_000L
    }
}
