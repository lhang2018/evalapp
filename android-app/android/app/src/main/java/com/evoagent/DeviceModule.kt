package com.evoagent

import android.app.Activity
import android.content.Intent
import android.database.Cursor
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.provider.OpenableColumns
import android.provider.Settings
import android.util.Base64
import java.io.ByteArrayOutputStream
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

    /** 诊断包通过系统文件选择器保存；写入在后台线程完成，取消不算异常。 */
    @ReactMethod
    fun saveDiagnosticsPackage(fileName: String, base64: String, promise: Promise) {
        val activity = currentActivity
        if (activity == null) {
            promise.resolve(failure("当前没有可用界面"))
            return
        }
        val name = fileName.substringAfterLast('/').substringAfterLast('\\')
        if (!name.lowercase().endsWith(".zip") || name.length > 128 ||
            !name.matches(Regex("^[A-Za-z0-9._-]+$"))
        ) {
            promise.resolve(failure("文件名不合法"))
            return
        }
        if (savePromise != null) {
            promise.resolve(failure("已有导出任务在进行中"))
            return
        }
        val bytes = try {
            Base64.decode(base64, Base64.DEFAULT)
        } catch (error: Exception) {
            null
        }
        if (bytes == null || bytes.isEmpty()) {
            promise.resolve(failure("导出内容为空"))
            return
        }
        savePromise = promise
        saveBytes = bytes
        try {
            val intent = Intent(Intent.ACTION_CREATE_DOCUMENT).apply {
                addCategory(Intent.CATEGORY_OPENABLE)
                type = "application/zip"
                putExtra(Intent.EXTRA_TITLE, name)
            }
            activity.startActivityForResult(intent, REQUEST_SAVE_DIAGNOSTICS)
        } catch (error: Exception) {
            savePromise = null
            saveBytes = null
            promise.resolve(failure(error.message ?: "无法打开文件选择器"))
        }
    }

    /** 诊断包导入：系统文件选择器只读取字节，不上传；凭证与目标由 JS 侧决定。 */
    @ReactMethod
    fun pickDiagnosticsPackage(promise: Promise) {
        val activity = currentActivity
        if (activity == null) {
            promise.resolve(failure("当前没有可用界面"))
            return
        }
        if (openPromise != null) {
            promise.resolve(failure("已有导入选择在进行中"))
            return
        }
        openPromise = promise
        try {
            val intent = Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
                addCategory(Intent.CATEGORY_OPENABLE)
                type = "application/zip"
                putExtra(
                    Intent.EXTRA_MIME_TYPES,
                    arrayOf("application/zip", "application/octet-stream"),
                )
            }
            activity.startActivityForResult(intent, REQUEST_OPEN_DIAGNOSTICS)
        } catch (error: Exception) {
            openPromise = null
            promise.resolve(failure(error.message ?: "无法打开文件选择器"))
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
        const val REQUEST_SAVE_DIAGNOSTICS = 4701
        const val REQUEST_OPEN_DIAGNOSTICS = 4702
        private const val CLICK_SETTLE_MS = 700L
        private const val LAUNCH_SETTLE_MS = 1200L
        private const val MAX_IMPORT_BYTES = 20 * 1024 * 1024
        private var savePromise: Promise? = null
        private var saveBytes: ByteArray? = null
        private var openPromise: Promise? = null

        /** 由 MainActivity.onActivityResult 调用；返回 true 表示已消费该结果。 */
        fun completeSave(
            activity: Activity,
            requestCode: Int,
            resultCode: Int,
            data: Intent?,
        ): Boolean {
            if (requestCode != REQUEST_SAVE_DIAGNOSTICS) return false
            val promise = savePromise ?: return true
            val bytes = saveBytes
            savePromise = null
            saveBytes = null
            val uri = data?.data
            if (resultCode != Activity.RESULT_OK || uri == null || bytes == null) {
                promise.resolve(saveFailure("已取消保存"))
                return true
            }
            Thread {
                try {
                    val stream = activity.contentResolver.openOutputStream(uri, "wt")
                        ?: throw IllegalStateException("无法打开目标文件")
                    stream.use { it.write(bytes) }
                    promise.resolve(Arguments.createMap().apply {
                        putBoolean("ok", true)
                        putString("message", "已保存诊断包")
                        putString("uri", uri.toString())
                    })
                } catch (error: Exception) {
                    promise.resolve(saveFailure(error.message ?: "写入失败"))
                }
            }.start()
            return true
        }

        /** 读取系统文件选择器结果；大小上限与空包在此处拦截。 */
        fun completeOpen(
            activity: Activity,
            requestCode: Int,
            resultCode: Int,
            data: Intent?,
        ): Boolean {
            if (requestCode != REQUEST_OPEN_DIAGNOSTICS) return false
            val promise = openPromise ?: return true
            openPromise = null
            val uri = data?.data
            if (resultCode != Activity.RESULT_OK || uri == null) {
                promise.resolve(saveFailure("已取消选择"))
                return true
            }
            Thread {
                try {
                    val name = displayName(activity, uri) ?: "diagnostics.zip"
                    val bytes = activity.contentResolver
                        .openInputStream(uri)?.use { stream ->
                            val buffer = ByteArrayOutputStream()
                            val chunk = ByteArray(64 * 1024)
                            var total = 0
                            while (true) {
                                val read = stream.read(chunk)
                                if (read <= 0) break
                                total += read
                                if (total > MAX_IMPORT_BYTES) {
                                    throw IllegalStateException("导入包超过 20 MiB")
                                }
                                buffer.write(chunk, 0, read)
                            }
                            buffer.toByteArray()
                        } ?: throw IllegalStateException("无法读取选中文件")
                    if (bytes.isEmpty()) {
                        throw IllegalStateException("导入包为空")
                    }
                    promise.resolve(Arguments.createMap().apply {
                        putBoolean("ok", true)
                        putString("file_name", name)
                        putDouble("size", bytes.size.toDouble())
                        putString(
                            "base64",
                            Base64.encodeToString(bytes, Base64.NO_WRAP),
                        )
                        putString("message", "已读取诊断包")
                    })
                } catch (error: Exception) {
                    promise.resolve(saveFailure(error.message ?: "读取失败"))
                }
            }.start()
            return true
        }

        private fun displayName(activity: Activity, uri: Uri): String? {
            val cursor: Cursor = activity.contentResolver.query(
                uri,
                arrayOf(OpenableColumns.DISPLAY_NAME),
                null,
                null,
                null,
            ) ?: return null
            return cursor.use {
                if (!it.moveToFirst()) {
                    null
                } else {
                    val index = it.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                    if (index < 0) null else it.getString(index)
                }
            }
        }

        private fun saveFailure(message: String) = Arguments.createMap().apply {
            putBoolean("ok", false)
            putString("message", message)
        }
    }
}
