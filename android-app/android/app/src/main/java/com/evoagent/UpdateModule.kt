package com.evoagent

import android.os.StatFs
import android.util.Base64
import com.facebook.react.bridge.Arguments
import com.facebook.react.bridge.Promise
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.react.bridge.ReactContextBaseJavaModule
import com.facebook.react.bridge.ReactMethod
import java.io.File
import java.io.FileOutputStream

/**
 * 更新制品暂存端口：只允许应用私有目录内的相对路径。
 * JS 侧先写 `.part`，摘要通过后再 rename 为正式制品。
 */
class UpdateModule(
    private val reactContext: ReactApplicationContext,
) : ReactContextBaseJavaModule(reactContext) {

    override fun getName(): String = "EvoAgentUpdate"

    private fun resolve(path: String): File {
        if (path.isBlank() || path.startsWith("/") || path.contains("..")) {
            throw IllegalArgumentException("非法制品路径: $path")
        }
        val root = reactContext.filesDir.canonicalFile
        val file = File(root, path).canonicalFile
        if (file != root && !file.path.startsWith(root.path + File.separator)) {
            throw IllegalArgumentException("制品路径越界: $path")
        }
        return file
    }

    @ReactMethod
    fun stat(path: String, promise: Promise) {
        try {
            val file = resolve(path)
            if (!file.isFile) {
                promise.resolve(null)
                return
            }
            promise.resolve(Arguments.createMap().apply {
                putDouble("size", file.length().toDouble())
            })
        } catch (error: Exception) {
            promise.reject("stat_failed", error.message, error)
        }
    }

    @ReactMethod
    fun append(path: String, base64: String, promise: Promise) {
        try {
            val file = resolve(path)
            file.parentFile?.mkdirs()
            val bytes = Base64.decode(base64, Base64.DEFAULT)
            FileOutputStream(file, true).use { stream ->
                stream.write(bytes)
                stream.flush()
                stream.fd.sync()
            }
            promise.resolve(true)
        } catch (error: Exception) {
            promise.reject("append_failed", error.message, error)
        }
    }

    @ReactMethod
    fun read(path: String, promise: Promise) {
        try {
            val file = resolve(path)
            if (!file.isFile) throw IllegalStateException("文件不存在: $path")
            promise.resolve(
                Base64.encodeToString(file.readBytes(), Base64.NO_WRAP))
        } catch (error: Exception) {
            promise.reject("read_failed", error.message, error)
        }
    }

    @ReactMethod
    fun rename(from: String, to: String, promise: Promise) {
        try {
            val source = resolve(from)
            val target = resolve(to)
            if (!source.isFile) throw IllegalStateException("源文件不存在: $from")
            target.parentFile?.mkdirs()
            if (!source.renameTo(target)) {
                throw IllegalStateException("原子改名失败: $from -> $to")
            }
            promise.resolve(true)
        } catch (error: Exception) {
            promise.reject("rename_failed", error.message, error)
        }
    }

    @ReactMethod
    fun remove(path: String, promise: Promise) {
        try {
            val file = resolve(path)
            promise.resolve(!file.exists() || file.delete())
        } catch (error: Exception) {
            promise.reject("remove_failed", error.message, error)
        }
    }

    @ReactMethod
    fun freeBytes(promise: Promise) {
        try {
            val stat = StatFs(reactContext.filesDir.absolutePath)
            promise.resolve(stat.availableBytes.toDouble())
        } catch (error: Exception) {
            promise.reject("free_bytes_failed", error.message, error)
        }
    }
}
