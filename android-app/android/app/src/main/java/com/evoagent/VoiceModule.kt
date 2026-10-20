package com.evoagent

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.speech.RecognitionListener
import android.speech.RecognitionService
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import com.facebook.react.bridge.Arguments
import com.facebook.react.bridge.Promise
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.react.bridge.ReactContextBaseJavaModule
import com.facebook.react.bridge.ReactMethod
import java.util.Locale

/**
 * 语音输入与语音反馈（PRD 64/69/89.1）。
 *
 * 只调用系统 SpeechRecognizer / TextToSpeech：麦克风权限由用户显式授予，
 * 识别文本仅作为任务输入，不自动提交；无服务时返回 unavailable，不尝试绕过系统限制。
 */
class VoiceModule(
    private val reactContext: ReactApplicationContext,
) : ReactContextBaseJavaModule(reactContext) {

    private val mainHandler = Handler(Looper.getMainLooper())

    private var recognizer: SpeechRecognizer? = null
    private var recognizePromise: Promise? = null
    private var recognitionSession = 0

    private var tts: TextToSpeech? = null
    private var ttsState = TTS_IDLE
    private var pendingSpeak: Promise? = null
    private var pendingSpeakText: String? = null
    private var currentUtterance: String? = null
    private var utteranceCounter = 0

    override fun getName(): String = "EvoAgentVoice"

    @ReactMethod
    fun voiceStatus(promise: Promise) {
        val recognitionAvailable = try {
            SpeechRecognizer.isRecognitionAvailable(reactContext)
        } catch (error: Exception) {
            false
        }
        val ttsEngine = tts?.defaultEngine ?: firstServicePackage(TTS_SERVICE_ACTION)
        val recognitionEngine = firstServicePackage(RecognitionService.SERVICE_INTERFACE)
        promise.resolve(Arguments.createMap().apply {
            putBoolean("available", recognitionAvailable || ttsEngine != null)
            putBoolean("recognition_available", recognitionAvailable)
            putBoolean("tts_available", ttsEngine != null)
            putBoolean("permission_granted", hasAudioPermission())
            putString("recognition_engine", recognitionEngine)
            putString("tts_engine", ttsEngine)
        })
    }

    @ReactMethod
    fun startVoiceInput(timeoutMs: Double, promise: Promise) {
        if (!hasAudioPermission()) {
            promise.resolve(transcriptionResult("denied", null, "未授权麦克风：请在系统权限中允许录音"))
            return
        }
        if (recognizePromise != null) {
            promise.resolve(transcriptionResult("failed", null, "已有语音识别在进行中"))
            return
        }
        val available = try {
            SpeechRecognizer.isRecognitionAvailable(reactContext)
        } catch (error: Exception) {
            false
        }
        if (!available) {
            promise.resolve(transcriptionResult("unavailable", null, "系统没有可用的语音识别服务"))
            return
        }

        val timeout = clampTimeout(timeoutMs)
        val session = ++recognitionSession
        recognizePromise = promise

        mainHandler.post {
            if (session != recognitionSession) return@post
            try {
                val speech = SpeechRecognizer.createSpeechRecognizer(reactContext)
                recognizer = speech
                speech.setRecognitionListener(listener(session))
                speech.startListening(recognizerIntent())
                mainHandler.postDelayed({
                    if (session == recognitionSession && recognizePromise != null) {
                        finishRecognition(session, "empty", null, "未在规定时间内识别到语音")
                    }
                }, timeout)
            } catch (error: Exception) {
                finishRecognition(session, "failed", null, error.message ?: "无法启动语音识别")
            }
        }
    }

    @ReactMethod
    fun cancelVoiceInput(promise: Promise) {
        val pending = recognizePromise
        if (pending == null) {
            promise.resolve(Arguments.createMap())
            return
        }
        recognizePromise = null
        recognitionSession++
        val speech = recognizer
        recognizer = null
        mainHandler.post {
            try {
                speech?.cancel()
                speech?.destroy()
            } catch (error: Exception) {
                // 已失效的识别器无需处理
            }
        }
        pending.resolve(transcriptionResult("cancelled", null, "已取消语音输入"))
        promise.resolve(Arguments.createMap())
    }

    @ReactMethod
    fun speak(text: String, promise: Promise) {
        val clipped = text.trim().take(MAX_SPEAK_CHARS)
        if (clipped.isEmpty()) {
            promise.resolve(speakResult("failed", "没有可朗读的内容"))
            return
        }
        if (pendingSpeak != null) {
            promise.resolve(speakResult("failed", "已有朗读在进行中"))
            return
        }
        if (ttsState == TTS_FAILED) {
            promise.resolve(speakResult("unavailable", "系统语音合成不可用"))
            return
        }
        pendingSpeak = promise
        pendingSpeakText = clipped
        when (ttsState) {
            TTS_READY -> startUtterance(clipped)
            TTS_INITIALIZING -> Unit
            else -> initTts()
        }
    }

    @ReactMethod
    fun stopSpeaking(promise: Promise) {
        if (pendingSpeak != null) {
            finishSpeak("cancelled", "已停止朗读")
        }
        tts?.stop()
        promise.resolve(speakResult("cancelled", "已停止朗读"))
    }

    override fun invalidate() {
        mainHandler.removeCallbacksAndMessages(null)
        recognitionSession++
        val pendingRecognition = recognizePromise
        recognizePromise = null
        val speech = recognizer
        recognizer = null
        pendingRecognition?.resolve(transcriptionResult("cancelled", null, "模块已释放"))

        val pendingSpeech = pendingSpeak
        pendingSpeak = null
        pendingSpeakText = null
        pendingSpeech?.resolve(speakResult("cancelled", "模块已释放"))

        val engine = tts
        tts = null
        ttsState = TTS_IDLE
        mainHandler.post {
            try {
                speech?.cancel()
                speech?.destroy()
            } catch (error: Exception) {
                // 已失效的识别器无需处理
            }
            try {
                engine?.stop()
                engine?.shutdown()
            } catch (error: Exception) {
                // 已失效的合成器无需处理
            }
        }
        super.invalidate()
    }

    private fun listener(session: Int) = object : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) = Unit
        override fun onBeginningOfSpeech() = Unit
        override fun onRmsChanged(rmsdB: Float) = Unit
        override fun onBufferReceived(buffer: ByteArray?) = Unit
        override fun onEndOfSpeech() = Unit
        override fun onPartialResults(partialResults: Bundle?) = Unit
        override fun onEvent(eventType: Int, params: Bundle?) = Unit

        override fun onResults(results: Bundle?) {
            val transcript = results
                ?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
                ?.firstOrNull()
                ?.trim()
                .orEmpty()
            if (transcript.isEmpty()) {
                finishRecognition(session, "empty", null, "没有识别到语音内容")
            } else {
                finishRecognition(session, "transcribed", transcript, "已识别语音，可编辑后提交")
            }
        }

        override fun onError(error: Int) {
            val mapped = recognitionError(error)
            finishRecognition(session, mapped.first, null, mapped.second)
        }
    }

    private fun finishRecognition(
        session: Int,
        status: String,
        transcript: String?,
        message: String,
    ) {
        if (session != recognitionSession) return
        recognitionSession++
        val promise = recognizePromise
        recognizePromise = null
        val speech = recognizer
        recognizer = null
        mainHandler.post {
            try {
                speech?.destroy()
            } catch (error: Exception) {
                // 已失效的识别器无需处理
            }
        }
        promise?.resolve(transcriptionResult(status, transcript, message))
    }

    private fun initTts() {
        ttsState = TTS_INITIALIZING
        mainHandler.post {
            try {
                val engine = TextToSpeech(reactContext) { status ->
                    if (status != TextToSpeech.SUCCESS) {
                        failTts("无法初始化系统语音合成")
                        return@TextToSpeech
                    }
                    val current = tts ?: run {
                        failTts("系统语音合成不可用")
                        return@TextToSpeech
                    }
                    val language = current.setLanguage(Locale.getDefault())
                    val supported = language != TextToSpeech.LANG_MISSING_DATA &&
                        language != TextToSpeech.LANG_NOT_SUPPORTED
                    if (!supported) {
                        failTts("系统缺少可用的语音合成语言包")
                        return@TextToSpeech
                    }
                    current.setOnUtteranceProgressListener(progressListener)
                    ttsState = TTS_READY
                    val queued = pendingSpeakText
                    if (queued != null) startUtterance(queued)
                }
                tts = engine
            } catch (error: Exception) {
                failTts(error.message ?: "无法初始化系统语音合成")
            }
        }
    }

    private fun startUtterance(text: String) {
        val engine = tts ?: run {
            finishSpeak("unavailable", "系统语音合成不可用")
            return
        }
        val utterance = "evoagent-${++utteranceCounter}"
        currentUtterance = utterance
        val result = engine.speak(text, TextToSpeech.QUEUE_FLUSH, null, utterance)
        if (result != TextToSpeech.SUCCESS) {
            finishSpeak("failed", "无法开始朗读")
        }
    }

    private fun failTts(message: String) {
        ttsState = TTS_FAILED
        val engine = tts
        tts = null
        try {
            engine?.shutdown()
        } catch (error: Exception) {
            // 已失效的合成器无需处理
        }
        finishSpeak("unavailable", message)
    }

    private fun finishSpeak(status: String, message: String) {
        val promise = pendingSpeak ?: return
        pendingSpeak = null
        pendingSpeakText = null
        currentUtterance = null
        promise.resolve(speakResult(status, message))
    }

    private val progressListener = object : UtteranceProgressListener() {
        override fun onStart(utteranceId: String?) = Unit

        override fun onDone(utteranceId: String?) {
            if (utteranceId == currentUtterance) {
                finishSpeak("spoken", "已朗读")
            }
        }

        @Deprecated("Deprecated in Java")
        override fun onError(utteranceId: String?) {
            if (utteranceId == currentUtterance) {
                finishSpeak("failed", "语音合成失败")
            }
        }
    }

    private fun recognizerIntent(): Intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
        putExtra(
            RecognizerIntent.EXTRA_LANGUAGE_MODEL,
            RecognizerIntent.LANGUAGE_MODEL_FREE_FORM,
        )
        putExtra(RecognizerIntent.EXTRA_LANGUAGE, Locale.getDefault().toLanguageTag())
        putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
        putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, false)
        putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, reactContext.packageName)
    }

    private fun recognitionError(error: Int): Pair<String, String> = when (error) {
        SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS ->
            "denied" to "麦克风权限被拒绝"
        SpeechRecognizer.ERROR_NO_MATCH,
        SpeechRecognizer.ERROR_SPEECH_TIMEOUT ->
            "empty" to "没有识别到语音内容"
        SpeechRecognizer.ERROR_NETWORK,
        SpeechRecognizer.ERROR_NETWORK_TIMEOUT,
        SpeechRecognizer.ERROR_SERVER,
        SpeechRecognizer.ERROR_LANGUAGE_NOT_SUPPORTED,
        SpeechRecognizer.ERROR_LANGUAGE_UNAVAILABLE ->
            "unavailable" to "语音识别服务暂时不可用"
        SpeechRecognizer.ERROR_RECOGNIZER_BUSY ->
            "failed" to "识别服务正忙，请稍后重试"
        SpeechRecognizer.ERROR_AUDIO ->
            "failed" to "无法读取麦克风音频"
        else -> "failed" to "语音识别失败（错误码 $error）"
    }

    private fun clampTimeout(value: Double): Long {
        if (!value.isFinite()) return DEFAULT_TIMEOUT_MS
        return value
            .coerceIn(MIN_TIMEOUT_MS.toDouble(), MAX_TIMEOUT_MS.toDouble())
            .toLong()
    }

    private fun hasAudioPermission(): Boolean =
        reactContext.checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED

    private fun firstServicePackage(action: String): String? = try {
        reactContext.packageManager
            .queryIntentServices(Intent(action), 0)
            .firstOrNull()
            ?.serviceInfo
            ?.packageName
    } catch (error: Exception) {
        null
    }

    private fun transcriptionResult(
        status: String,
        transcript: String?,
        message: String,
    ) = Arguments.createMap().apply {
        putString("status", status)
        putString("transcript", transcript)
        putString("message", message)
    }

    private fun speakResult(status: String, message: String) =
        Arguments.createMap().apply {
            putString("status", status)
            putString("message", message)
        }

    companion object {
        private const val TTS_SERVICE_ACTION = "android.intent.action.TTS_SERVICE"
        private const val MIN_TIMEOUT_MS = 3_000L
        private const val MAX_TIMEOUT_MS = 30_000L
        private const val DEFAULT_TIMEOUT_MS = 10_000L
        private const val MAX_SPEAK_CHARS = 1_200

        private const val TTS_IDLE = "idle"
        private const val TTS_INITIALIZING = "initializing"
        private const val TTS_READY = "ready"
        private const val TTS_FAILED = "failed"
    }
}
