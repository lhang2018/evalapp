package com.evoagent

import android.content.Intent
import android.net.Uri
import android.provider.Settings
import com.facebook.react.bridge.Arguments
import com.facebook.react.bridge.Promise
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.react.bridge.ReactContextBaseJavaModule
import com.facebook.react.bridge.ReactMethod

/** JS 契约：{ok, evidence?, message?}；失败不抛异常，返回结构化原因。 */
class DeviceModule(
    private val reactContext: ReactApplicationContext,
) : ReactContextBaseJavaModule(reactContext) {

    override fun getName(): String = "EvoAgentDevice"

    @ReactMethod
    fun isServiceEnabled(promise: Promise) {
        promise.resolve(isAccessibilityServiceEnabled())
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
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            reactContext.startActivity(intent)
            val evidence = Arguments.createMap().apply {
                putString("launched", target)
                putBoolean("resolved", true)
            }
            promise.resolve(success(evidence, "已发送启动请求"))
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
            val clicked = service.clickByText(text)
            if (!clicked) {
                promise.resolve(failure("未找到可点击节点: $text"))
                return
            }
            val evidence = Arguments.createMap().apply {
                putString("clicked_text", text)
                service.activeWindowPackage()?.let { putString("package_name", it) }
            }
            promise.resolve(success(evidence, "已点击"))
        } catch (error: Exception) {
            promise.resolve(failure(error.message ?: "点击失败"))
        }
    }

    private fun isAccessibilityServiceEnabled(): Boolean {
        val enabled = Settings.Secure.getString(
            reactContext.contentResolver,
            Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES,
        ) ?: return false
        val expected =
            "${reactContext.packageName}/${DeviceAccessibilityService::class.java.name}"
        return enabled.split(':').any { it.equals(expected, ignoreCase = true) }
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
}
