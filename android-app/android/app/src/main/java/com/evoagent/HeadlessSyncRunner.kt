package com.evoagent

import android.content.Context
import android.os.Handler
import android.os.Looper
import com.facebook.react.ReactApplication
import com.facebook.react.ReactInstanceEventListener
import com.facebook.react.bridge.Arguments
import com.facebook.react.bridge.ReactContext
import com.facebook.react.jstasks.HeadlessJsTaskConfig
import com.facebook.react.jstasks.HeadlessJsTaskContext
import com.facebook.react.jstasks.HeadlessJsTaskEventListener
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

/**
 * 在 WorkManager 的任务窗口内直接运行无界面 JS，不依赖前台服务。
 * Android 12+ 可能拒绝后台启动前台服务，而 JobScheduler 的任务窗口足以完成短同步。
 */
object HeadlessSyncRunner {
    const val TASK_KEY = "EvoAgentBackgroundSync"

    fun run(context: Context, timeoutMs: Long): Boolean {
        val app = context.applicationContext as? ReactApplication ?: return false
        val manager = app.reactNativeHost.reactInstanceManager
        val main = Handler(Looper.getMainLooper())
        val ready = CountDownLatch(1)

        main.post {
            if (manager.currentReactContext != null) {
                ready.countDown()
                return@post
            }
            val listener = object : ReactInstanceEventListener {
                override fun onReactContextInitialized(reactContext: ReactContext) {
                    manager.removeReactInstanceEventListener(this)
                    ready.countDown()
                }
            }
            manager.addReactInstanceEventListener(listener)
            manager.createReactContextInBackground()
        }

        if (!ready.await(timeoutMs, TimeUnit.MILLISECONDS)) return false
        val reactContext = manager.currentReactContext ?: return false

        val finished = CountDownLatch(1)
        val runningTaskId = AtomicInteger(-1)
        val startRequested = AtomicBoolean(false)
        val taskContext = HeadlessJsTaskContext.getInstance(reactContext)
        val listener = object : HeadlessJsTaskEventListener {
            override fun onHeadlessJsTaskStart(id: Int) {
                if (startRequested.get()) runningTaskId.compareAndSet(-1, id)
            }

            override fun onHeadlessJsTaskFinish(id: Int) {
                if (id == runningTaskId.get()) finished.countDown()
            }
        }
        taskContext.addTaskEventListener(listener)
        try {
            main.post {
                try {
                    startRequested.set(true)
                    taskContext.startTask(
                        HeadlessJsTaskConfig(
                            TASK_KEY,
                            Arguments.createMap(),
                            timeoutMs,
                            true,
                        ),
                    )
                } catch (error: Exception) {
                    finished.countDown()
                }
            }
            return finished.await(timeoutMs, TimeUnit.MILLISECONDS)
        } catch (error: InterruptedException) {
            Thread.currentThread().interrupt()
            return false
        } finally {
            taskContext.removeTaskEventListener(listener)
        }
    }
}
