package org.lastphone.app

import android.Manifest
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.provider.Settings
import android.util.Base64
import android.view.View
import android.view.ViewGroup
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.*
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.webkit.JavaScriptReplyProxy
import androidx.webkit.ScriptHandler
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import org.json.JSONObject
import java.io.ByteArrayInputStream
import java.io.File
import java.util.concurrent.atomic.AtomicInteger

class MainActivity : ComponentActivity() {
    lateinit var web: WebView
        private set
    val host: EngineHost get() = (application as LastphoneApplication).engine
    private lateinit var status: TextView
    private lateinit var files: PortableFiles
    private var documentScript: ScriptHandler? = null
    private val posts = AtomicInteger()
    private var treeCallback: ((String?) -> Unit)? = null
    private var requireLibrary = true
    private var fileCallback: ((String?) -> Unit)? = null
    private var pendingAccess: (() -> Unit)? = null
    private var pendingExport: Triple<ByteArray, String, JavaScriptReplyProxy>? = null
    private var fullScreen: View? = null
    private var fullScreenCallback: WebChromeClient.CustomViewCallback? = null

    private val treePicker = registerForActivityResult(ActivityResultContracts.OpenDocumentTree()) { uri ->
        if (uri == null) { treeCallback?.invoke(null); treeCallback = null }
        else chooseTree(uri)
    }
    private val filePicker = registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri == null) { fileCallback?.invoke(null); fileCallback = null }
        else {
            val callback = fileCallback; fileCallback = null
            showStatus("Copying the selected file…")
            host.worker.execute {
                try {
                    val copied = files.importFile(uri, File(host.dataRoot, "imports")) { text -> runOnUiThread { showStatus(text) } }
                    runOnUiThread { showStatus(""); callback?.invoke(copied.absolutePath) }
                } catch (error: Throwable) { runOnUiThread { showError("File import failed", error); callback?.invoke(null) } }
            }
        }
    }
    private val accessScreen = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) {
        val action = pendingAccess; pendingAccess = null
        if (Build.VERSION.SDK_INT >= 30 && Environment.isExternalStorageManager()) action?.invoke()
        else { treeCallback?.invoke(null); treeCallback = null; showStatus("Folder access was not granted. You can import a copy instead.") }
    }
    private val legacyAccess = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        val action = pendingAccess; pendingAccess = null
        if (granted) action?.invoke() else { treeCallback?.invoke(null); treeCallback = null }
    }
    private val exportPicker = registerForActivityResult(ActivityResultContracts.CreateDocument("text/markdown")) { uri ->
        val export = pendingExport; pendingExport = null
        if (export != null) host.worker.execute {
            try {
                if (uri == null) throw IllegalStateException("Export cancelled")
                contentResolver.openOutputStream(uri, "w")?.use { it.write(export.first) }
                    ?: throw IllegalStateException("Cannot write this file")
                reply(export.third, JSONObject().put("id", export.second).put("ok", true))
            } catch (error: Throwable) { reply(export.third, JSONObject().put("id", export.second).put("error", error.message)) }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        host.activity = this
        files = PortableFiles(this)
        val layout = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        val toolbar = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; setPadding(dp(12), 0, dp(6), 0) }
        toolbar.addView(TextView(this).apply { text = "Lastphone"; textSize = 18f; setTextColor(Color.rgb(24, 63, 50)) },
            LinearLayout.LayoutParams(0, dp(48), 1f))
        toolbar.addView(Button(this).apply { text = "Settings"; contentDescription = "Lastphone settings"; setOnClickListener { showMenu() } },
            LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, dp(48)))
        layout.addView(toolbar)
        status = TextView(this).apply { textSize = 14f; setPadding(dp(16), dp(8), dp(16), dp(8)); visibility = View.GONE }
        layout.addView(status)
        web = WebView(this)
        web.contentDescription = "Offline library"
        layout.addView(web, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        setContentView(layout)
        ViewCompat.setOnApplyWindowInsetsListener(layout) { view, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
            val keyboard = insets.getInsets(WindowInsetsCompat.Type.ime())
            view.setPadding(bars.left, bars.top, bars.right, maxOf(bars.bottom, keyboard.bottom))
            insets
        }
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (fullScreen != null) hideFullScreen()
                else if (web.canGoBack()) web.goBack()
                else { isEnabled = false; onBackPressedDispatcher.onBackPressed() }
            }
        })
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER) ||
            !WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) {
            showStatus("Update Android System WebView to open Lastphone. The installed WebView does not support its secure local bridge.")
            return
        }
        configureWebView()
        showStatus("Opening the offline library…")
        host.start().whenComplete { _, error -> runOnUiThread {
            if (isDestroyed) return@runOnUiThread
            if (error != null) showError("Lastphone could not start", error) else reloadEnginePage()
        } }
    }

    @Suppress("SetJavaScriptEnabled")
    private fun configureWebView() {
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG)
        web.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            allowFileAccess = false
            allowContentAccess = false
            setGeolocationEnabled(false)
            mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            cacheMode = WebSettings.LOAD_NO_CACHE
            blockNetworkLoads = true
            mediaPlaybackRequiresUserGesture = true
            setSupportMultipleWindows(false)
            builtInZoomControls = true
            displayZoomControls = false
        }
        web.webChromeClient = object : WebChromeClient() {
            override fun onShowCustomView(view: View, callback: CustomViewCallback) {
                if (fullScreen != null) { callback.onCustomViewHidden(); return }
                fullScreen = view; fullScreenCallback = callback
                view.setBackgroundColor(Color.BLACK)
                view.keepScreenOn = true
                (window.decorView as ViewGroup).addView(view, ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
                androidx.core.view.WindowCompat.getInsetsController(window, view).hide(WindowInsetsCompat.Type.systemBars())
            }
            override fun onHideCustomView() { hideFullScreen() }
        }
        web.webViewClient = object : WebViewClient() {
            override fun shouldInterceptRequest(view: WebView?, request: WebResourceRequest): WebResourceResponse {
                val url = request.url.toString()
                if (!sameOrigin(request.url) || request.method !in listOf("GET", "HEAD")) return refused()
                return try { host.request(request.method, url, request.requestHeaders, document = if (request.isForMainFrame) true else null).webResponse() }
                catch (error: Throwable) { refused(500, "Local request failed") }
            }
            override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest): Boolean {
                val uri = request.url
                if (!sameOrigin(uri)) return true
                if (!request.isForMainFrame) {
                    val path = uri.path ?: ""
                    return !listOf("/documents/", "/pages/", "/videos/", "/videos-external/", "/transcripts/", "/metadata/videos/", "/courses/", "/collections/", "/assets/", "/zim/").any { path.startsWith(it) }
                }
                if (uri.path in listOf("", "/", "/ui/setup.html")) return false
                navigateInside(uri.toString())
                return true
            }
            override fun onPageFinished(view: WebView?, url: String?) { showStatus("") }
        }
        WebViewCompat.addWebMessageListener(web, "LastphoneNative", setOf(EngineHost.ORIGIN)) { _, message, origin, isMainFrame, proxy ->
            if (!isMainFrame || !sameOrigin(origin)) return@addWebMessageListener
            val raw = message.data ?: return@addWebMessageListener
            if (raw.length > 12 * 1024 * 1024) return@addWebMessageListener
            val data = try { JSONObject(raw) } catch (_: Exception) { return@addWebMessageListener }
            val id = data.optString("id")
            if (id.length !in 1..64) return@addWebMessageListener
            when (data.optString("action")) {
                "request" -> handlePost(data, proxy)
                "save" -> {
                    if (pendingExport != null) { reply(proxy, JSONObject().put("id", id).put("error", "Finish the previous export first")); return@addWebMessageListener }
                    try {
                        val bytes = Base64.decode(data.getString("body"), Base64.NO_WRAP)
                        require(bytes.size <= 8 * 1024 * 1024)
                        val name = PortableFiles.safeName(data.optString("name", "lastphone-export.md"))
                        pendingExport = Triple(bytes, id, proxy)
                        exportPicker.launch(name)
                    } catch (error: Exception) { reply(proxy, JSONObject().put("id", id).put("error", "Invalid export")) }
                }
                "navigate" -> { navigateInside(data.optString("url")); reply(proxy, JSONObject().put("id", id).put("ok", true)) }
                "ticket" -> host.worker.execute {
                    try {
                        host.call("grant_navigation", data.getString("url"))
                        reply(proxy, JSONObject().put("id", id).put("ok", true))
                    } catch (_: Throwable) { reply(proxy, JSONObject().put("id", id).put("error", "Cannot open this document")) }
                }
            }
        }
    }

    private fun handlePost(data: JSONObject, proxy: JavaScriptReplyProxy) {
        val id = data.getString("id")
        if (posts.incrementAndGet() > 48) {
            posts.decrementAndGet(); reply(proxy, JSONObject().put("id", id).put("error", "Too many requests")); return
        }
        val url = data.optString("url")
        val path = Uri.parse(url).path
        if (path in listOf("/__bridge/stream/start", "/api/setup/start", "/api/setup/resume", "/api/setup/retry")) startWork()
        host.worker.execute {
            try {
                require(sameOrigin(Uri.parse(url)) && data.optString("method") == "POST") { "Invalid request origin or method" }
                val rawHeaders = data.getJSONObject("headers")
                val headers = mutableMapOf<String, String>()
                rawHeaders.keys().forEach { headers[it] = rawHeaders.getString(it) }
                headers["Origin"] = EngineHost.ORIGIN
                val body = Base64.decode(data.getString("body"), Base64.NO_WRAP)
                require(body.size <= 256 * 1024) { "Request is too large" }
                host.request("POST", url, headers, body).use { response ->
                    reply(proxy, JSONObject().put("id", id).put("status", response.status).put("reason", response.reason)
                        .put("headers", JSONObject(response.headers)).put("body", Base64.encodeToString(response.bytes(), Base64.NO_WRAP)))
                }
            } catch (error: Throwable) { reply(proxy, JSONObject().put("id", id).put("error", error.cause?.message ?: error.message ?: "Local request failed")) }
            finally { posts.decrementAndGet() }
        }
    }

    private fun reply(proxy: JavaScriptReplyProxy, value: JSONObject) { runOnUiThread { if (!isDestroyed) proxy.postMessage(value.toString()) } }
    private fun sameOrigin(uri: Uri) = uri.scheme == "https" && uri.host == "lastphone.invalid" && uri.port == -1 && uri.userInfo == null
    private fun refused(code: Int = 403, message: String = "Network access is blocked") = WebResourceResponse(
        "text/plain", "utf-8", code, if (code == 403) "Forbidden" else "Error", mapOf("Cache-Control" to "no-store"), ByteArrayInputStream(message.toByteArray()))

    fun reloadEnginePage() {
        host.worker.execute {
            try {
                val script = assets.open("android-transport.js").bufferedReader().use { it.readText() } + "\n" + host.topScript()
                val url = host.startUrl()
                runOnUiThread {
                    if (isDestroyed) return@runOnUiThread
                    documentScript?.remove()
                    documentScript = WebViewCompat.addDocumentStartJavaScript(web, script, setOf(EngineHost.ORIGIN))
                    web.loadUrl(url)
                }
            } catch (error: Throwable) { runOnUiThread { showError("Cannot open the library", error) } }
        }
    }
    private fun navigateInside(url: String) {
        if (!sameOrigin(Uri.parse(url))) return
        host.worker.execute {
            try {
                val route = JSONObject(host.call("navigation", url, true, true).toString())
                if (route.optString("action") == "inside") runOnUiThread { web.loadUrl(route.getString("url")) }
            } catch (_: Throwable) { /* Untrusted library links cannot trigger another app. */ }
        }
    }
    fun showStatus(message: String) { status.text = message; status.visibility = if (message.isBlank()) View.GONE else View.VISIBLE }
    fun showError(title: String, error: Throwable) {
        showStatus("")
        AlertDialog.Builder(this).setTitle(title).setMessage(error.cause?.message ?: error.message ?: error.javaClass.simpleName).setPositiveButton("Close", null).show()
    }
    private fun dp(value: Int) = (value * resources.displayMetrics.density).toInt()

    private fun showMenu() {
        AlertDialog.Builder(this).setTitle("Lastphone").setItems(arrayOf(
            "Open a library folder", "Local AI", "Library downloads and repair", "Share on Wi-Fi", "Diagnostics and licenses"
        )) { _, item -> when (item) {
            0 -> { requireLibrary = true; treeCallback = { path -> if (path != null) host.openLibrary(File(path)) }; treePicker.launch(null) }
            1 -> showModelSettings()
            2 -> web.loadUrl(EngineHost.ORIGIN + "/ui/setup.html#manage")
            3 -> showLan()
            4 -> host.worker.execute {
                val details = try { host.call("diagnostics").toString() } catch (_: Exception) { "Lastphone ${BuildConfig.VERSION_NAME}\nData: ${host.dataRoot}" }
                runOnUiThread { AlertDialog.Builder(this).setTitle("Lastphone ${BuildConfig.VERSION_NAME}").setMessage(details + "\n\nAndroid distribution: GPL-3.0-or-later. Original Lastbook code: MIT. Full source and dependency notices: github.com/qqrm/lastphone.").setPositiveButton("Close", null).show() }
            }
        } }.show()
    }

    fun pickForEngine(files: Boolean, callback: (String?) -> Unit) {
        if (treeCallback != null || fileCallback != null) { callback(null); return }
        if (files) { fileCallback = callback; filePicker.launch(arrayOf("*/*")) }
        else { requireLibrary = false; treeCallback = callback; treePicker.launch(null) }
    }

    private fun chooseTree(uri: Uri) {
        val shared = files.sharedDirectory(uri)
        val choices = if (shared != null) arrayOf("Use this folder in place", "Import a separate copy") else arrayOf("Import a separate copy")
        AlertDialog.Builder(this).setTitle("Portable library")
            .setMessage("Use in place keeps the library in your folder and needs file access. Import copies it into Lastphone's storage; allow enough free space.")
            .setPositiveButton(choices[0]) { _, _ -> if (shared != null) useSharedFolder(shared) else copyTree(uri) }
            .apply { if (shared != null) setNeutralButton(choices[1]) { _, _ -> copyTree(uri) } }
            .setNegativeButton("Cancel") { _, _ -> treeCallback?.invoke(null); treeCallback = null }
            .setOnCancelListener { treeCallback?.invoke(null); treeCallback = null }.show()
    }
    private fun useSharedFolder(folder: File) {
        val open = {
            val callback = treeCallback; treeCallback = null
            host.worker.execute {
                val root = PortableFiles.libraryRoot(folder) ?: folder
                try {
                    require(root.isDirectory && root.canRead() && root.canWrite()) { "This folder is not readable and writable" }
                    if (requireLibrary) require(PortableFiles.libraryRoot(root) != null) { "This is not a Lastbook data folder" }
                    runOnUiThread { callback?.invoke(root.absolutePath) }
                } catch (error: Throwable) { runOnUiThread { showError("Cannot use this folder", error); callback?.invoke(null) } }
            }
        }
        if (Build.VERSION.SDK_INT >= 30 && !Environment.isExternalStorageManager()) {
            pendingAccess = open
            accessScreen.launch(Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION, Uri.parse("package:$packageName")))
        } else if (Build.VERSION.SDK_INT == 29 && ContextCompat.checkSelfPermission(this, Manifest.permission.WRITE_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED) {
            pendingAccess = open; legacyAccess.launch(Manifest.permission.WRITE_EXTERNAL_STORAGE)
        } else open()
    }
    private fun copyTree(uri: Uri) {
        val callback = treeCallback; treeCallback = null
        showStatus("Checking the selected folder…")
        host.worker.execute {
            try {
                val imported = files.importTree(uri, requireLibrary) { text -> runOnUiThread { showStatus(text) } }
                runOnUiThread { showStatus(""); callback?.invoke(imported.absolutePath) }
            } catch (error: Throwable) { runOnUiThread { showError("Folder import failed", error); callback?.invoke(null) } }
        }
    }

    private fun showModelSettings() {
        host.worker.execute {
            try {
                val settings = JSONObject(host.call("model_settings").toString())
                runOnUiThread { modelDialog(settings) }
            } catch (error: Throwable) { runOnUiThread { showError("Cannot read AI settings", error) } }
        }
    }
    private fun modelDialog(settings: JSONObject) {
        val layout = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(20), dp(8), dp(20), 0) }
        val enabled = CheckBox(this).apply { text = "Enable on-device AI"; isChecked = settings.optBoolean("enabled", true) }
        val mode = Spinner(this).apply {
            adapter = ArrayAdapter(this@MainActivity, android.R.layout.simple_spinner_dropdown_item, arrayOf("GGUF inside Lastphone", "Local OpenAI-compatible server"))
            setSelection(if (settings.optString("mode") == "loopback") 1 else 0)
        }
        val path = EditText(this).apply { hint = "Path to a GGUF model in your data folder"; setText(settings.optString("path")); isSingleLine = false }
        val backend = Spinner(this).apply {
            adapter = ArrayAdapter(this@MainActivity, android.R.layout.simple_spinner_dropdown_item, arrayOf("CPU", "Vulkan GPU"))
            setSelection(if (settings.optString("backend") == "vulkan") 1 else 0)
        }
        fun number(label: String, value: Int): EditText {
            layout.addView(TextView(this).apply { text = label })
            return EditText(this).apply { inputType = android.text.InputType.TYPE_CLASS_NUMBER; setText(value.toString()); layout.addView(this) }
        }
        layout.addView(enabled); layout.addView(TextView(this).apply { text = "Model runtime" }); layout.addView(mode)
        layout.addView(TextView(this).apply { text = "Selected GGUF model" }); layout.addView(path)
        layout.addView(Button(this).apply { text = "Import GGUF from a file"; setOnClickListener {
            fileCallback = { selected -> if (selected != null) path.setText(selected) }; filePicker.launch(arrayOf("*/*"))
        } })
        layout.addView(TextView(this).apply { text = "Inference backend" }); layout.addView(backend)
        val models = settings.optJSONArray("available_models")
        val profile = Spinner(this)
        if (models != null && models.length() > 0) {
            val titles = (0 until models.length()).map { models.getJSONObject(it).getString("title") }
            profile.adapter = ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, titles)
            profile.setSelection((0 until models.length()).firstOrNull { models.getJSONObject(it).getString("id") == settings.optString("setup_model") } ?: 0)
            layout.addView(TextView(this).apply { text = "Model offered by Library downloads" }); layout.addView(profile)
        }
        val context = number("Context tokens", settings.optInt("context", 4096))
        val threads = number("CPU threads", settings.optInt("threads", 4))
        val layers = number("GPU layers (Vulkan)", settings.optInt("gpu_layers", 99))
        layout.addView(TextView(this).apply { text = "Optional server on this phone (loopback only)" })
        val httpBase = EditText(this).apply { hint = "http://127.0.0.1:8080"; setText(settings.optString("http_base", "http://127.0.0.1:8080")); inputType = android.text.InputType.TYPE_TEXT_VARIATION_URI; layout.addView(this) }
        val httpModel = EditText(this).apply { hint = "Server model name"; setText(settings.optString("http_model", "local")); layout.addView(this) }
        val scroll = ScrollView(this).apply { addView(layout) }
        AlertDialog.Builder(this).setTitle("Local AI").setView(scroll).setPositiveButton("Save") { _, _ ->
            val updated = JSONObject().put("enabled", enabled.isChecked).put("path", path.text.toString().trim())
                .put("mode", if (mode.selectedItemPosition == 1) "loopback" else "gguf")
                .put("http_base", httpBase.text.toString().trim()).put("http_model", httpModel.text.toString().trim())
                .put("backend", if (backend.selectedItemPosition == 1) "vulkan" else "cpu")
                .put("context", context.text.toString().toIntOrNull() ?: 4096)
                .put("threads", threads.text.toString().toIntOrNull() ?: 4)
                .put("gpu_layers", layers.text.toString().toIntOrNull() ?: 99)
            if (models != null && models.length() > 0) updated.put("setup_model", models.getJSONObject(profile.selectedItemPosition).getString("id"))
            host.worker.execute {
                try { host.call("set_model_settings", updated.toString()); runOnUiThread { web.reload() } }
                catch (error: Throwable) { runOnUiThread { showError("AI settings were not saved", error) } }
            }
        }.setNegativeButton("Cancel", null).show()
    }
    private fun showLan() {
        AlertDialog.Builder(this).setTitle("Share the library on Wi-Fi")
            .setMessage("Start sharing only on a network you trust. Anyone with the access link can read this library until you stop sharing. The access key changes each session.")
            .setPositiveButton("Start sharing") { _, _ ->
                startWork()
                host.worker.execute {
                    try {
                        val info = host.call("start_lan").toString()
                        runOnUiThread { AlertDialog.Builder(this).setTitle("Library sharing").setMessage(info).setPositiveButton("Close", null).show() }
                    } catch (error: Throwable) { runOnUiThread { showError("Cannot start sharing", error) } }
                }
            }.setNeutralButton("Stop sharing") { _, _ -> host.worker.execute { host.call("stop_lan") } }
            .setNegativeButton("Cancel", null).show()
    }
    private fun startWork() { ContextCompat.startForegroundService(this, Intent(this, WorkService::class.java)) }
    private fun hideFullScreen() {
        fullScreen?.let { it.keepScreenOn = false; (it.parent as? ViewGroup)?.removeView(it) }
        fullScreen = null
        fullScreenCallback?.onCustomViewHidden(); fullScreenCallback = null
        androidx.core.view.WindowCompat.getInsetsController(window, web).show(WindowInsetsCompat.Type.systemBars())
    }
    override fun onPause() { super.onPause(); web.onPause() }
    override fun onResume() { super.onResume(); if (::web.isInitialized) web.onResume(); host.activity = this }
    override fun onDestroy() {
        treeCallback?.invoke(null); fileCallback?.invoke(null)
        if (host.activity === this) host.activity = null
        documentScript?.remove()
        hideFullScreen()
        if (::web.isInitialized) { web.stopLoading(); web.destroy() }
        super.onDestroy()
    }
}
