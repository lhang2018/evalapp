package com.evoagent

import android.accessibilityservice.AccessibilityService
import android.graphics.Rect
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import com.facebook.react.bridge.Arguments
import com.facebook.react.bridge.WritableArray
import com.facebook.react.bridge.WritableMap

/**
 * 节点级操作服务：只匹配文本/描述并执行 ACTION_CLICK，不注入坐标手势，
 * 仅在用户于系统设置中显式启用后工作。界面快照只回传前台 JS 做结果校验，
 * 不落盘、不上传。
 */
class DeviceAccessibilityService : AccessibilityService() {

    override fun onServiceConnected() {
        instance = this
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        // 被动事件不落盘；操作结果由前台 JS 主动查询
    }

    override fun onInterrupt() {}

    override fun onDestroy() {
        if (instance === this) instance = null
        super.onDestroy()
    }

    fun clickByText(text: String): Boolean {
        val target = text.trim()
        if (target.isBlank()) return false
        val root = rootInActiveWindow ?: return false
        val exact = mutableListOf<AccessibilityNodeInfo>()
        val partial = mutableListOf<AccessibilityNodeInfo>()
        collectMatches(root, target, exact, partial)
        // 精确匹配优先："设置" 不应命中 "打开系统无障碍设置"
        return clickFirst(exact) || clickFirst(partial)
    }

    private fun clickFirst(matches: List<AccessibilityNodeInfo>): Boolean {
        for (node in matches) {
            if (node.isClickable && node.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
                return true
            }
            var parent = node.parent
            while (parent != null) {
                if (parent.isClickable &&
                    parent.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                ) {
                    return true
                }
                parent = parent.parent
            }
        }
        return false
    }

    fun activeWindowPackage(): String? =
        rootInActiveWindow?.packageName?.toString()

    /** 读取当前窗口的节点树（有数量/深度上限），用于操作前后对比。 */
    fun captureSnapshot(): WritableMap {
        val root = rootInActiveWindow
        val result = Arguments.createMap()
        result.putString("package_name", root?.packageName?.toString())
        result.putDouble("captured_at", System.currentTimeMillis().toDouble())
        val counter = intArrayOf(0)
        val truncated = booleanArrayOf(false)
        val nodes = Arguments.createArray()
        if (root != null) {
            nodes.pushMap(nodeToMap(root, 0, counter, truncated))
        }
        result.putArray("nodes", nodes)
        result.putInt("node_count", counter[0])
        result.putBoolean("truncated", truncated[0])
        return result
    }

    private fun nodeToMap(
        node: AccessibilityNodeInfo,
        depth: Int,
        counter: IntArray,
        truncated: BooleanArray,
    ): WritableMap {
        counter[0] += 1
        val map = Arguments.createMap()
        map.putString("text", node.text?.toString()?.take(MAX_TEXT_CHARS))
        map.putString(
            "desc",
            node.contentDescription?.toString()?.take(MAX_TEXT_CHARS),
        )
        map.putString("role", node.className?.toString())
        map.putBoolean("clickable", node.isClickable)
        map.putBoolean("enabled", node.isEnabled)
        map.putBoolean("visible", node.isVisibleToUser)
        val bounds = Rect()
        node.getBoundsInScreen(bounds)
        map.putArray(
            "bounds",
            Arguments.createArray().apply {
                pushInt(bounds.left)
                pushInt(bounds.top)
                pushInt(bounds.right)
                pushInt(bounds.bottom)
            },
        )
        val children: WritableArray = Arguments.createArray()
        if (depth < MAX_DEPTH) {
            for (index in 0 until node.childCount) {
                if (counter[0] >= MAX_NODES) {
                    truncated[0] = true
                    break
                }
                val child = node.getChild(index) ?: continue
                children.pushMap(nodeToMap(child, depth + 1, counter, truncated))
            }
        } else if (node.childCount > 0) {
            truncated[0] = true
        }
        map.putArray("children", children)
        return map
    }

    private fun collectMatches(
        node: AccessibilityNodeInfo?,
        text: String,
        exact: MutableList<AccessibilityNodeInfo>,
        partial: MutableList<AccessibilityNodeInfo>,
    ) {
        if (node == null) return
        val label = node.text?.toString()
        val description = node.contentDescription?.toString()
        if (label == text || description == text) {
            exact.add(node)
        } else if ((label != null && label.contains(text)) ||
            (description != null && description.contains(text))
        ) {
            partial.add(node)
        }
        for (index in 0 until node.childCount) {
            collectMatches(node.getChild(index), text, exact, partial)
        }
    }

    companion object {
        @Volatile
        var instance: DeviceAccessibilityService? = null
            private set

        private const val MAX_NODES = 300
        private const val MAX_DEPTH = 12
        private const val MAX_TEXT_CHARS = 200
    }
}
