package com.evoagent

import android.content.Intent
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import com.facebook.react.bridge.Arguments
import com.facebook.react.bridge.Promise
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.react.bridge.ReactContextBaseJavaModule
import com.facebook.react.bridge.ReactMethod

/**
 * JS 契约：{ok, evidence?, message?}；失败不抛异常，返回结构化原因。
 * 操作证据携带前后界面快照，由 JS 侧判定是否真正生效。
 */
class DeviceModule(
    private val reactContext: ReactApplicationContext,
) : ReactContextBaseJavaModule(reactContext) {

    override fun getName(): String = "EvoAgentDevice"

    @ReactMethod
    fun isServiceEnabled(promise: Promise) {
        promise.resolve(isAccessibilityServiceEnabled())
    }

    @ReactMethod
    fun snapshot(promise: Promise) {
        val service = DeviceAccessibilityService.instance
        if (service == null) {
            promise.resolve(failure("无障碍服务未启用"))
            return
        }
        try {
            promise.resolve(success(service.captureSnapshot(), "已读取当前界面"))
        } catch (error: Exception) {
            promise.resolve(failure(error.message ?: "读取界面失败"))
        }
    }

    @ReactMethod
    fun launchIntent(target: String, promise: Promise) {
        try {
            val manager = reactContext.packageManager
            val intent = manager.getLaunchIntentForPackage(target)
                ?: if (target.startsWith("http://") ||
                    target.startsWith("https://") ||
                    target.contains("://")
                ) {
                    Intent(Intent.ACTION_VIEW, Uri.parse(target))
                } else {
                    null
                }
            if (intent == null || intent.resolveActivity(manager) == null) {
                promise.resolve(failure("无法解析目标: $target"))
                return
            }
            val service = DeviceAccessibilityService.instance
            val before = capture(service)
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            reactContext.startActivity(intent)
            Handler(Looper.getMainLooper()).postDelayed({
                val after = capture(service)
                val evidence = Arguments.createMap().apply {
                    putString("launched", target)
                    putBoolean("resolved", true)
                    before?.let { putMap("before", it) }
                    after?.let { putMap("after", it) }
                }
                promise.resolve(success(evidence, "已发送启动请求，等待界面确认"))
            }, LAUNCH_SETTLE_MS)
        } catch (error: Exception) {
            promise.resolve(failure(error.message ?: "启动失败"))
        }
    }

    @ReactMethod
    fun performClick(text: String, promise: Promise) {
        val service = DeviceAccessibilityService.instance
        if (service == null) {
            promise.resolve(failure("无障碍服务未启用"))
            return
        }
        if (text.isBlank()) {
            promise.resolve(failure("target 不能为空"))
            return
        }
        try {
            val before = capture(service)
            val clicked = service.clickByText(text)
            if (!clicked) {
                promise.resolve(failure("未找到可点击节点: $text"))
                return
            }
            Handler(Looper.getMainLooper()).postDelayed({
                val after = capture(service)
                val evidence = Arguments.createMap().apply {
                    putString("clicked_text", text)
                    service.activeWindowPackage()?.let { putString("package_name", it) }
                    before?.let { putMap("before", it) }
                    after?.let { putMap("after", it) }
                }
                promise.resolve(success(evidence, "已点击，等待界面确认"))
            }, CLICK_SETTLE_MS)
        } catch (error: Exception) {
            promise.resolve(failure(error.message ?: "点击失败"))
        }
    }

    private fun capture(
        service: DeviceAccessibilityService?,
    ): com.facebook.react.bridge.WritableMap? = try {
        service?.captureSnapshot()
    } catch (error: Exception) {
        null
    }

    private fun isAccessibilityServiceEnabled(): Boolean {
        // 已连接的服务实例是最可靠的事实来源；部分 ROM 的 secure 设置格式不同
        if (DeviceAccessibilityService.instance != null) return true
        val enabled = Settings.Secure.getString(
            reactContext.contentResolver,
            Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES,
        ) ?: return false
        val pageName = DeviceAccessibilityService::class.java.name
        return enabled.split(':').any {
            it.equals("${reactContext.packageName}/$pageName", ignoreCase = true) ||
                (it.contains(reactContext.packageName) && it.contains("DeviceAccessibilityService"))
        }
    }

    private fun success(
        evidence: com.facebook.react.bridge.WritableMap,
        message: String,
    ) = Arguments.createMap().apply {
        putBoolean("ok", true)
        putMap("evidence", evidence)
        putString("message", message)
    }

    private fun failure(message: String) = Arguments.createMap().apply {
        putBoolean("ok", false)
        putString("message", message)
    }

    companion object {
        private const val CLICK_SETTLE_MS = 700L
        private const val LAUNCH_SETTLE_MS = 1200L
    }
}
