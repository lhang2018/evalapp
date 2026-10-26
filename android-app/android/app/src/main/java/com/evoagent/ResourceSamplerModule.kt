package com.evoagent

import android.app.ActivityManager
import android.content.Context
import android.os.BatteryManager
import android.os.StatFs
import com.facebook.react.bridge.Arguments
import com.facebook.react.bridge.Promise
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.react.bridge.ReactContextBaseJavaModule
import com.facebook.react.bridge.ReactMethod
import java.io.File
import kotlin.math.max
import kotlin.math.min

/** ALN-15-E：原生资源采样（CPU/内存/电量/磁盘），带来源、单位、采样时间与
 * 不可用状态。采样失败返回 valid=false 与 errors，**绝不伪造成低压力**。 */
class ResourceSamplerModule(
    reactContext: ReactApplicationContext,
) : ReactContextBaseJavaModule(reactContext) {
    private data class CpuSnapshot(val idle: Long, val total: Long)

    private var lastCpu: CpuSnapshot? = null

    override fun getName(): String = "ResourceSampler"

    private fun readCpuSnapshot(): CpuSnapshot? {
        return try {
            val line = File("/proc/stat").readLines().firstOrNull {
                it.startsWith("cpu ")
            } ?: return null
            val parts = line.trim().split(Regex("\\s+"))
            if (parts.size < 5) return null
            val values = parts.drop(1).map { it.toLongOrNull() ?: return null }
            val idle = (values.getOrElse(3) { 0L }) + (values.getOrElse(4) { 0L })
            CpuSnapshot(idle = idle, total = values.sum())
        } catch (error: Exception) {
            null
        }
    }

    private fun cpuFraction(): Double? {
        val current = readCpuSnapshot() ?: return null
        val previous = lastCpu
        lastCpu = current
        if (previous == null) return null // 首次无基线：不伪造低压力
        val totalDelta = current.total - previous.total
        if (totalDelta <= 0) return null
        val idleDelta = current.idle - previous.idle
        return min(1.0, max(0.0, 1.0 - idleDelta.toDouble() / totalDelta.toDouble()))
    }

    private fun memoryFraction(): Double? {
        return try {
            val manager = reactApplicationContext
                .getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
            val info = ActivityManager.MemoryInfo()
            manager.getMemoryInfo(info)
            if (info.totalMem <= 0L) return null
            min(1.0, max(0.0, (info.totalMem - info.availMem).toDouble() /
                info.totalMem.toDouble()))
        } catch (error: Exception) {
            null
        }
    }

    private fun batteryFraction(): Double? {
        return try {
            val manager = reactApplicationContext
                .getSystemService(Context.BATTERY_SERVICE) as BatteryManager
            val level = manager.getIntProperty(
                BatteryManager.BATTERY_PROPERTY_CAPACITY,
            )
            if (level < 0) null else min(1.0, max(0.0, level / 100.0))
        } catch (error: Exception) {
            null
        }
    }

    private fun diskFraction(): Double? {
        return try {
            val stats = StatFs(reactApplicationContext.filesDir.absolutePath)
            val total = stats.blockCountLong.toDouble() * stats.blockSizeLong
            if (total <= 0.0) return null
            val free = stats.availableBlocksLong.toDouble() * stats.blockSizeLong
            min(1.0, max(0.0, 1.0 - free / total))
        } catch (error: Exception) {
            null
        }
    }

    @ReactMethod
    fun sample(promise: Promise) {
        val errors = Arguments.createArray()
        val cpu = try { cpuFraction() } catch (e: Exception) { errors.pushString("cpu:${e.message}"); null }
        val memory = try { memoryFraction() } catch (e: Exception) { errors.pushString("memory:${e.message}"); null }
        val battery = try { batteryFraction() } catch (e: Exception) { errors.pushString("battery:${e.message}"); null }
        val disk = try { diskFraction() } catch (e: Exception) { errors.pushString("disk:${e.message}"); null }
        if (cpu == null) errors.pushString("cpu_unavailable_or_baseline_missing")
        if (memory == null) errors.pushString("memory_unavailable")
        val payload = Arguments.createMap().apply {
            putBoolean("valid", cpu != null && memory != null)
            putDouble("sampledAtMs", System.currentTimeMillis().toDouble())
            putString("source", "/proc/stat+ActivityManager+BatteryManager+StatFs")
            cpu?.let { putDouble("cpuFraction", it) }
            memory?.let { putDouble("memoryFraction", it) }
            battery?.let { putDouble("batteryFraction", it) }
            disk?.let { putDouble("diskFraction", it) }
            putArray("errors", errors)
            putMap("units", Arguments.createMap().apply {
                putString("cpuFraction", "fraction")
                putString("memoryFraction", "fraction")
                putString("batteryFraction", "fraction")
                putString("diskFraction", "fraction")
                putString("sampledAtMs", "epoch_ms")
            })
        }
        promise.resolve(payload)
    }
}
