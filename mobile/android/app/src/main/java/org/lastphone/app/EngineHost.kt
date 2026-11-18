package org.lastphone.app

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.webkit.WebResourceResponse
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONArray
import org.json.JSONObject
import org.lastphone.app.nativebridge.NativePlatform
import java.io.ByteArrayInputStream
import java.io.File
import java.io.FileInputStream
import java.io.FilterInputStream
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.Inet4Address
import java.net.NetworkInterface
import java.net.URL
import java.util.concurrent.CompletableFuture
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/** The original engine owns routes, budgets, validation, indexing and chat. */
class EngineHost(private val context: Context) {
    companion object {
        const val ORIGIN = "https://lastphone.invalid"
        // Load the APK's NDK runtime before Chaquopy or ReLinker can load an older copy.
        init { System.loadLibrary("c++_shared") }
    }
    val worker = Executors.newFixedThreadPool(8)
    val transfers = TransferControl()
    private val control = Executors.newSingleThreadExecutor()
    private val cancellationQueued = AtomicBoolean()
    val main = Handler(Looper.getMainLooper())
    val preferences = context.getSharedPreferences("lastphone", Context.MODE_PRIVATE)
    val native = NativePlatform(context)
    @Volatile var activity: MainActivity? = null
    @Volatile private var initialized: CompletableFuture<PyObject>? = null
    val callbacks = PlatformCallbacks()
    val dataRoot: File get() = File(preferences.getString("data_root", null)
        ?: File(context.getExternalFilesDir(null) ?: context.filesDir, "LastphoneData").absolutePath)

    @Synchronized fun start(): CompletableFuture<PyObject> {
        initialized?.let { return it }
        val future = CompletableFuture<PyObject>()
        initialized = future
        worker.execute {
            try {
                PortableFiles(context).cleanupInterruptedCopies()
                if (!Python.isStarted()) Python.start(AndroidPlatform(context))
                // Reinstalling the same development version must refresh packaged web assets too.
                val installedAt = context.packageManager.getPackageInfo(context.packageName, 0).lastUpdateTime
                val program = File(context.filesDir, "engine-${BuildConfig.VERSION_CODE}-$installedAt")
                if (!File(program, ".complete").exists()) {
                    val pending = File(context.filesDir, program.name + ".pending")
                    pending.deleteRecursively()
                    copyAssetTree("engine", pending)
                    File(pending, ".complete").writeText(BuildConfig.VERSION_NAME)
                    check(!program.exists() || program.deleteRecursively()) { "Cannot replace program assets" }
                    check(pending.renameTo(program)) { "Cannot activate program assets" }
                    context.filesDir.listFiles()?.filter { it != program && it.isDirectory && Regex("engine-[0-9]+(?:-[0-9]+)?").matches(it.name) }
                        ?.forEach { it.deleteRecursively() }
                }
                dataRoot.mkdirs()
                val settings = File(context.filesDir, "settings").apply { mkdirs() }
                val engine = Python.getInstance().getModule("lastphone_android").callAttr(
                    "configure", program.absolutePath, dataRoot.absolutePath, settings.absolutePath, native, callbacks
                )
                future.complete(engine)
            } catch (error: Throwable) { future.completeExceptionally(error) }
        }
        return future
    }

    fun engine(): PyObject = start().get(180, TimeUnit.SECONDS)
    fun call(name: String, vararg args: Any?): PyObject = engine().callAttr(name, *args)
    /** A Stop action must not queue behind eight long-poll requests or imports. */
    fun cancelWork() {
        transfers.cancel()
        if (cancellationQueued.compareAndSet(false, true)) control.execute {
            try {
                try { native.llmCancel() } catch (_: Throwable) { }
                try { call("cancel_work") } catch (_: Throwable) { }
            } finally { cancellationQueued.set(false) }
        }
    }
    fun startUrl(): String = engine().get("start_url")!!.toString()
    fun topScript(): String = call("top_script").toString()

    fun request(method: String, url: String, headers: Map<String, String> = emptyMap(), body: ByteArray? = null, document: Boolean? = null): EngineResponse {
        return EngineResponse(call("request", method, url, JSONObject(headers).toString(), body, document))
    }

    fun openLibrary(folder: File) {
        worker.execute {
            try {
                call("open_library", folder.absolutePath)
                preferences.edit().putString("data_root", folder.absolutePath).apply()
                main.post { activity?.reloadEnginePage() }
            } catch (error: Throwable) { main.post { activity?.showError("Cannot open this library", error) } }
        }
    }

    fun copyAssetTree(path: String, target: File) {
        val children = context.assets.list(path) ?: emptyArray()
        if (children.isEmpty()) {
            target.parentFile?.mkdirs()
            context.assets.open(path).use { input -> target.outputStream().use { input.copyTo(it) } }
        } else {
            target.mkdirs()
            children.forEach { child -> copyAssetTree("$path/$child", File(target, child)) }
        }
    }

    /** Called on an engine worker; the Android picker always runs on the main thread. */
    inner class PlatformCallbacks {
        fun appVersion(): String = BuildConfig.VERSION_NAME
        fun localAddresses(): String = JSONArray(NetworkInterface.getNetworkInterfaces().toList()
            .filter { it.isUp && !it.isLoopback }
            .flatMap { it.inetAddresses.toList() }.filterIsInstance<Inet4Address>()
            .filter { !it.isLoopbackAddress && !it.isLinkLocalAddress }.map { it.hostAddress }).toString()
        /** Only invoked by the original update-check button. No redirects, cookies or telemetry. */
        fun checkUpdate(): String {
            val connection = URL("https://api.github.com/repos/qqrm/lastphone/releases/latest").openConnection() as HttpURLConnection
            try {
                connection.instanceFollowRedirects = false
                connection.connectTimeout = 15000
                connection.readTimeout = 15000
                connection.setRequestProperty("User-Agent", "Lastphone/${BuildConfig.VERSION_NAME}")
                connection.setRequestProperty("Accept", "application/vnd.github+json")
                val status = connection.responseCode
                val stream = if (status in 200..299) connection.inputStream else connection.errorStream
                val body = stream?.use {
                    val output = java.io.ByteArrayOutputStream()
                    val buffer = ByteArray(8192)
                    while (true) {
                        val count = it.read(buffer)
                        if (count < 0) break
                        check(output.size() + count <= 256 * 1024) { "Update response is too large" }
                        output.write(buffer, 0, count)
                    }
                    output.toString("UTF-8")
                } ?: ""
                return JSONObject().put("status", status).put("body", body).toString()
            } finally { connection.disconnect() }
        }
        fun pickFolder(files: Boolean): String? {
            val screen = activity ?: return null
            val done = CountDownLatch(1)
            var selected: String? = null
            main.post { screen.pickForEngine(files) { selected = it; done.countDown() } }
            // Backgrounding or declining the picker does not block an engine lane forever.
            done.await(10, TimeUnit.MINUTES)
            return selected
        }
        fun openLibrary(path: String) { this@EngineHost.openLibrary(File(path)) }
    }
}

/** Holds the Python lane budget until WebView closes the response stream. */
class EngineResponse(private val value: PyObject) : AutoCloseable {
    val status = value.get("status")!!.toInt()
    val reason = value.get("reason")!!.toString().ifBlank { "Response" }
    val headers: MutableMap<String, String> = mutableMapOf()
    init {
        val json = JSONObject(value.get("headers_json")!!.toString())
        json.keys().forEach { key -> headers[key] = json.getString(key) }
    }
    private var closed = false
    private var opened: InputStream? = null
    fun stream(): InputStream {
        opened?.let { return it }
        val path = value.get("path")?.toJava(String::class.java)
        val source = if (path != null) FileInputStream(path) else ByteArrayInputStream(
            value.get("body")?.toJava(ByteArray::class.java) ?: ByteArray(0)
        )
        return object : FilterInputStream(source) {
            override fun close() {
                try { super.close() } finally { this@EngineResponse.close() }
            }
        }.also { opened = it }
    }
    fun bytes(): ByteArray = stream().use { it.readBytes() }
    fun webResponse(): WebResourceResponse {
        val contentType = headers.entries.firstOrNull { it.key.equals("content-type", true) }?.value ?: "application/octet-stream"
        val mime = contentType.substringBefore(';').trim()
        val encoding = Regex("charset=([^; ]+)", RegexOption.IGNORE_CASE).find(contentType)?.groupValues?.get(1)
        // WebResourceResponse forbids redirect status. Engine bridge resolves redirects in-process.
        require(status !in 300..399) { "Unresolved engine redirect: $status" }
        return WebResourceResponse(mime, encoding, status, reason, headers, stream())
    }
    @Synchronized override fun close() {
        if (!closed) { closed = true; value.callAttr("close") }
    }
}
