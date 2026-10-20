package com.evoagent

import android.content.Intent
import android.os.Bundle
import com.facebook.react.ReactActivity
import com.facebook.react.ReactActivityDelegate
import com.facebook.react.defaults.DefaultNewArchitectureEntryPoint.fabricEnabled
import com.facebook.react.defaults.DefaultReactActivityDelegate

class MainActivity : ReactActivity() {
    override fun getMainComponentName(): String = "EvoAgent"

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(null)
    }

    override fun createReactActivityDelegate(): ReactActivityDelegate =
        DefaultReactActivityDelegate(this, mainComponentName, fabricEnabled)

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        if (DeviceModule.completeSave(this, requestCode, resultCode, data)) return
        if (DeviceModule.completeOpen(this, requestCode, resultCode, data)) return
        super.onActivityResult(requestCode, resultCode, data)
    }
}
