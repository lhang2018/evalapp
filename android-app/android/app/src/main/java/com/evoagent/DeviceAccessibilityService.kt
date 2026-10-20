package com.evoagent

import android.accessibilityservice.AccessibilityService
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo

/**
 * 节点级操作服务：只匹配文本/描述并执行 ACTION_CLICK，不注入坐标手势，
 * 不读取或上传界面内容，仅在用户于系统设置中显式启用后工作。
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
        if (text.isBlank()) return false
        val roots = listOfNotNull(rootInActiveWindow)
        for (root in roots) {
            val matches = mutableListOf<AccessibilityNodeInfo>()
            collectMatches(root, text, matches)
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
        }
        return false
    }

    fun activeWindowPackage(): String? =
        rootInActiveWindow?.packageName?.toString()

    private fun collectMatches(
        node: AccessibilityNodeInfo?,
        text: String,
        out: MutableList<AccessibilityNodeInfo>,
    ) {
        if (node == null) return
        val label = node.text?.toString()
        val description = node.contentDescription?.toString()
        if ((label != null && label.contains(text)) ||
            (description != null && description.contains(text))
        ) {
            out.add(node)
        }
        for (index in 0 until node.childCount) {
            collectMatches(node.getChild(index), text, out)
        }
    }

    companion object {
        @Volatile
        var instance: DeviceAccessibilityService? = null
            private set
    }
}
