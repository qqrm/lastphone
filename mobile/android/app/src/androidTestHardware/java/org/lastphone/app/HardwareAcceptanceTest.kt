package org.lastphone.app

import android.app.ActivityManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.ConnectivityManager
import android.os.BatteryManager
import android.os.Build
import android.os.Debug
import android.os.PowerManager
import android.os.Process
import android.os.SystemClock
import android.system.Os
import android.system.OsConstants
import android.util.AtomicFile
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.lastphone.app.nativebridge.NativeLlama
import java.io.File
import java.io.FileOutputStream
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/** Only compiled into the isolated hardware flavor. Never substitutes CPU for a GPU pass. */
@RunWith(AndroidJUnit4::class)
class HardwareAcceptanceTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context = instrumentation.targetContext
    private val arguments = InstrumentationRegistry.getArguments()
    private val power = context.getSystemService(Context.POWER_SERVICE) as PowerManager
    private lateinit var report: JSONObject
    private lateinit var jsonFile: File
    private lateinit var eventFile: File
    private var recordCount = 0
    private var lastCheckpoint = 0L
    private var nativeUsed = false

    @Test fun hardwareSmoke() = withEvidence("hardwareSmoke") {
        val capabilities = requireTarget()
        val model = File(context.cacheDir, "hardware-stories260K.gguf")
        instrumentation.context.assets.open("models/stories260K.gguf").use { input ->
            model.outputStream().use { input.copyTo(it) }
        }
        try {
            val hash = sha256(model)
            assertEquals("The hardware fixture must be the revision-pinned GGUF.",
                "270cba1bd5109f42d03350f60406024560464db173c0e387d91f0426d3bd256d", hash)
            report.put("model", JSONObject().put("purpose", "integration_fixture")
                .put("sha256", hash).put("bytes", model.length()))
            val cpu = options("cpu", 256, 2)
            load(model, cpu, "cpu_unloaded")
            generate("cpu", "cpu_first", "Once upon a time", 16)
            val reused = generate("cpu", "cpu_reuse", "Once upon a time", 16)
            assertTrue("The repeated prompt must reuse its prefix.", reused.getInt("n_reused") > 0)
            cancelAndRecover("cpu", "Once upon a time")

            // -1 is llama.cpp's configured all-layers sentinel, not a measured
            // layer count. Request one layer here so this smoke check also has
            // an unambiguous positive configured offload count.
            assertTrue("Nubia acceptance requires a real Vulkan GPU; CPU fallback is a failure.",
                capabilities.getBoolean("vulkan"))
            val gpu = options("vulkan", 256, 2).put("n_gpu_layers", 1)
            load(model, gpu, "vulkan_one_layer_unloaded")
            generate("vulkan", "vulkan_first", "Once upon a time", 16)
            cancelAndRecover("vulkan", "Once upon a time")

            // Exercise the automatic fit selected by the shipping alpha too.
            load(model, options("vulkan", 256, 2), "vulkan_auto_unloaded")
            generate("vulkan", "vulkan_auto_first", "Once upon a time", 16)
            load(model, cpu, "cpu_after_vulkan")
            generate("cpu", "cpu_after_vulkan", "Once upon a time", 8)
        } finally { model.delete() }
    }

    @Test fun productionBenchmark() = withEvidence("productionBenchmark") {
        requireTarget()
        val modelArgument = requireNotNull(arguments.getString("hardwareModel")) {
            "productionBenchmark needs hardwareModel; it is not a fixture benchmark."
        }
        val model = File(modelArgument).canonicalFile
        require(model == File(context.filesDir, "hardware-model.gguf").canonicalFile) {
            "hardwareModel must be this isolated application's files/hardware-model.gguf."
        }
        require(model.isFile && model.length() > 0) { "The production GGUF is absent or empty." }
        val expectedHash = requireNotNull(arguments.getString("hardwareModelSha256")) {
            "hardwareModelSha256 is required."
        }.lowercase()
        require(Regex("[a-f0-9]{64}").matches(expectedHash)) { "Invalid model SHA-256." }
        val contextSize = integerArgument("hardwareContext", 2048, 256..32768)
        require(contextSize % 256 == 0) { "hardwareContext must be a multiple of 256 (native KV-cache alignment)." }
        val threads = integerArgument("hardwareThreads", 4, 1..16)
        val tokens = integerArgument("hardwareTokens", 128, 8..1024)
        require(tokens < contextSize / 2) { "hardwareTokens must be less than half hardwareContext." }
        val duration = integerArgument("hardwareDurationSeconds", 300, 0..1800)
        val repeats = integerArgument("hardwareRepeats", 3, 1..50)
        val order = (arguments.getString("hardwareBackendOrder") ?: "cpu,vulkan").split(',')
        require(order == listOf("cpu", "vulkan") || order == listOf("vulkan", "cpu")) {
            "hardwareBackendOrder must be cpu,vulkan or vulkan,cpu."
        }
        report.put("options", JSONObject().put("n_ctx", contextSize).put("n_threads", threads)
            .put("n_batch", 256).put("n_ubatch", 64)
            .put("max_tokens", tokens).put("duration_seconds_per_backend", duration)
            .put("minimum_initial_generations_per_backend", repeats).put("backend_order", JSONArray(order))
            .put("load_timeout_seconds", 300).put("generation_timeout_seconds", 180)
            .put("load_cache_scope", "model_unloaded; OS page cache is not evicted")
            .put("first_text_scope", "first visible UTF-8 callback chunk, not a native token boundary")
            .put("offload_scope", "configured layers and pre-load memory estimates; no measured GPU residency")
            .put("sampling_scope", "telemetry and evidence writes are outside each timed generation; sustained wall time includes them")
            .put("prompt_scope", "fixed raw completion; model answer quality and chat templates are not evaluated"))
        record("model_hash_started")
        val actualHash = sha256(model)
        report.put("model", JSONObject().put("purpose", "provided_production_model")
            .put("sha256", actualHash).put("bytes", model.length()))
        assertEquals("The copied GGUF does not match the host's selected model.", expectedHash, actualHash)
        checkpoint()

        // Instrumentation must remain runnable with the screen off, without
        // changing the user's display timeout or battery policy.
        val wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "Lastphone:hardware-test")
        wakeLock.acquire(TimeUnit.SECONDS.toMillis((2L * duration + 900L).coerceAtMost(4500L)))
        report.put("partial_wake_lock", true)
        val summaries = JSONArray()
        report.put("backends", summaries)
        try {
            for (backend in order) {
                requireThermalHeadroom()
                val configuration = options(backend, contextSize, threads)
                val loaded = load(model, configuration, "${backend}_unloaded")
                val prompt = benchmarkPrompt()
                val summary = JSONObject().put("backend", backend)
                    .put("configured_n_gpu_layers", loaded.getInt("n_gpu_layers"))
                    .put("initial_load", loaded).put("prompt_sha256", digest(prompt.toByteArray(Charsets.UTF_8)))
                    .put("initial_generations", JSONArray())
                summaries.put(summary)
                repeat(repeats) { index ->
                    requireThermalHeadroom()
                    val result = generate(backend, if (index == 0) "first_after_load" else "warm_prefix_reuse",
                        prompt, tokens, iteration = index)
                    if (index > 0) assertTrue("Identical prompts must retain a reusable prefix.", result.getInt("n_reused") > 0)
                    summary.getJSONArray("initial_generations").put(result)
                }
                cancelAndRecover(backend, prompt)
                val reload = load(model, configuration, "${backend}_reload_os_cache_uncontrolled")
                summary.put("reload", reload)

                val sustainedStart = SystemClock.elapsedRealtimeNanos()
                var iterations = 0
                var generated = 0L
                var nativeGenerationMs = 0L
                while (TimeUnit.NANOSECONDS.toSeconds(SystemClock.elapsedRealtimeNanos() - sustainedStart) < duration) {
                    requireThermalHeadroom()
                    // Changing the leading scenario avoids counting a cached
                    // identical prompt as sustained cold-prefill throughput.
                    val sustained = generate(backend, "sustained", benchmarkPrompt(iterations), tokens,
                        iteration = iterations)
                    generated += sustained.getInt("n_gen")
                    nativeGenerationMs += sustained.getLong("gen_ms")
                    iterations++
                }
                summary.put("sustained_iterations", iterations).put("sustained_generated_tokens", generated)
                    .put("sustained_native_generation_ms", nativeGenerationMs)
                    .put("sustained_elapsed_ms", elapsedMs(sustainedStart))
                    .put("sustained_status", if (duration > 0) "completed" else "not_requested")
                record("backend_complete", summary)
                NativeLlama.unload()
            }
        } finally { if (wakeLock.isHeld) wakeLock.release() }
    }

    private fun requireTarget(): JSONObject {
        assertEquals("Use the isolated hardware flavor, never the personal installation.",
            "io.github.qqrm.lastphone.hardwaretest", context.packageName)
        assertTrue("An ARM64 process is required.", Process.is64Bit() && Build.SUPPORTED_ABIS.contains("arm64-v8a"))
        val emulator = listOf(Build.HARDWARE, Build.MODEL, Build.DEVICE, Build.FINGERPRINT)
            .any { Regex("(?i)(goldfish|ranchu|emulator|sdk_gphone|generic)").containsMatchIn(it) }
        assertFalse("An emulator cannot satisfy Nubia hardware acceptance.", emulator)
        assertTrue("Expected Nubia Z60 Ultra NX721J/cerro.",
            listOf(Build.MODEL, Build.PRODUCT, Build.DEVICE).any {
                it.contains("NX721J", ignoreCase = true) || it.equals("cerro", ignoreCase = true)
            })
        requireOffline()
        nativeUsed = true
        val capabilities = JSONObject(NativeLlama.capabilities())
        report.put("capabilities", capabilities)
        record("capabilities", capabilities)
        assertTrue("CPU backend is missing.", capabilities.optBoolean("cpu"))
        assertTrue("A real Vulkan GPU must be available; CPU fallback cannot pass this test.",
            capabilities.optBoolean("vulkan"))
        return capabilities
    }

    private fun options(backend: String, contextSize: Int, threads: Int) = JSONObject()
        .put("backend", backend).put("n_ctx", contextSize).put("n_threads", threads)
        .put("n_batch", minOf(256, contextSize)).put("n_ubatch", minOf(64, contextSize))

    private fun load(model: File, options: JSONObject, stage: String): JSONObject {
        NativeLlama.unload()
        record("load_started", JSONObject().put("stage", stage).put("options", options))
        val start = SystemClock.elapsedRealtimeNanos()
        val loaded = boundedNativeCall(300) { JSONObject(NativeLlama.load(model.path, options.toString())) }
        loaded.put("wall_load_ms", elapsedMs(start)).put("stage", stage)
        record("load_result", loaded)
        assertEquals(loaded.toString(), "ready", loaded.optString("ev"))
        val backend = options.getString("backend")
        assertEquals(loaded.toString(), backend, loaded.getString("backend"))
        assertEquals("Fitting must not silently change the requested context.", options.getInt("n_ctx"), loaded.getInt("n_ctx"))
        assertEquals("Thread settings must remain identical across backends.", options.getInt("n_threads"), loaded.getInt("threads"))
        val layers = loaded.getInt("n_gpu_layers")
        if (backend == "cpu") {
            assertEquals("CPU must not offload model layers.", 0, layers)
        } else {
            assertTrue("GPU offload requires positive configured layers or the all-layers sentinel -1.", layers > 0 || layers == -1)
            if (options.has("n_gpu_layers")) assertEquals(options.getInt("n_gpu_layers"), layers)
            val fit = loaded.getJSONObject("fit")
            assertTrue("The fitter must estimate nonzero device allocation.", fit.getLong("device_bytes") > 0L)
            val rows = fit.getJSONArray("memory")
            assertTrue("The fitter must assign model bytes to a device, not only CPU memory.",
                (0 until rows.length()).map { rows.getJSONObject(it) }.any {
                    it.optString("kind") == "device" && it.optLong("model") > 0L
                })
            loaded.put("offload_estimate_accepted", true)
            loaded.put("configured_n_gpu_layers", layers)
        }
        return loaded
    }

    private fun generate(backend: String, stage: String, prompt: String, count: Int,
                         cancel: Boolean = false, iteration: Int = 0): JSONObject {
        val request = JSONObject().put("id", iteration).put("segments", JSONArray().put(JSONArray().put(prompt).put(false)))
            .put("max_tokens", count).put("temp", 0.0).put("seed", 1)
        record("generation_started", JSONObject().put("backend", backend).put("stage", stage).put("iteration", iteration))
        val start = SystemClock.elapsedRealtimeNanos()
        var firstTextAt = 0L
        var cancelAt = 0L
        var textChunks = 0
        var characters = 0L
        val result = boundedNativeCall(180) {
            JSONObject(NativeLlama.generate(request.toString()) { event ->
                val token = JSONObject(event)
                val text = token.getString("t")
                assertEquals("tok", token.getString("ev"))
                if (text.isNotEmpty()) {
                    if (firstTextAt == 0L) firstTextAt = SystemClock.elapsedRealtimeNanos()
                    textChunks++
                    characters += text.length
                    if (cancel && cancelAt == 0L) {
                        cancelAt = SystemClock.elapsedRealtimeNanos()
                        NativeLlama.cancel()
                    }
                }
            })
        }
        val end = SystemClock.elapsedRealtimeNanos()
        result.put("stage", stage).put("iteration", iteration).put("wall_generation_ms", nanosToMs(end - start))
            .put("first_text_ms", if (firstTextAt > 0) nanosToMs(firstTextAt - start) else JSONObject.NULL)
            .put("visible_utf8_chunks", textChunks).put("output_utf16_characters", characters)
        if (cancelAt > 0) result.put("cancel_to_return_ms", nanosToMs(end - cancelAt))
        val generationMs = result.optLong("gen_ms")
        result.put("native_generation_tokens_per_second", if (generationMs > 0)
            1000.0 * result.optInt("n_gen") / generationMs else JSONObject.NULL)
        record("generation_result", result)
        assertEquals(result.toString(), "done", result.optString("ev"))
        assertEquals(result.toString(), backend, result.getString("backend"))
        assertTrue("Real native generation must produce tokens.", result.getInt("n_gen") > 0)
        assertTrue("Real native generation must produce visible output.", characters > 0)
        if (cancel) {
            assertEquals("Cancellation must reach the native decode loop.", "cancel", result.getString("reason"))
            assertEquals("Cancellation follows the first visible chunk.", 1, textChunks)
        } else {
            assertNotEquals("Unexpected cancellation is not a successful benchmark.", "cancel", result.getString("reason"))
        }
        return result
    }

    private fun cancelAndRecover(backend: String, prompt: String) {
        generate(backend, "cancel_after_first_text", prompt, 32, cancel = true)
        generate(backend, "recover_after_cancel", prompt, 8)
    }

    private fun benchmarkPrompt(iteration: Int? = null): String {
        val prefix = if (iteration == null) "" else "Scenario $iteration. "
        // A fixed raw completion avoids silently depending on a particular
        // model's chat template. This measures execution, not answer quality.
        return prefix + "Write a practical guide for preparing an offline reference library. " +
            "Explain these topics in order: storage, documents, maps, search, backups, and verification. " +
            "Use clear numbered sections with examples.\n\n1. Storage\n"
    }

    private fun requireThermalHeadroom() {
        requireOffline()
        val status = power.currentThermalStatus
        if (status >= PowerManager.THERMAL_STATUS_CRITICAL) {
            record("thermal_abort", JSONObject().put("thermal_status", status))
            fail("Android reports CRITICAL or worse thermal status ($status); sustained run is incomplete.")
        }
    }

    private fun requireOffline() {
        val connectivity = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val hasNetwork = connectivity.activeNetwork != null
        report.put("offline_active_network", hasNetwork)
        assertFalse("Disable networking for offline acceptance; an active network remains.", hasNetwork)
    }

    private fun <T> boundedNativeCall(seconds: Long, body: () -> T): T {
        // One atomic transition prevents a just-expired previous timer from
        // cancelling the following request after this call has returned.
        val state = AtomicInteger(0) // 0 running, 1 completed, 2 expired
        val timer = Executors.newSingleThreadScheduledExecutor { runnable ->
            Thread(runnable, "lastphone-hardware-timeout").apply { isDaemon = true }
        }
        val deadline = timer.schedule({
            if (state.compareAndSet(0, 2)) NativeLlama.cancel()
        }, seconds, TimeUnit.SECONDS)
        try {
            val result = body()
            state.compareAndSet(0, 1)
            assertEquals("Native operation exceeded ${seconds}s; cancellation was requested.", 1, state.get())
            return result
        } finally { state.compareAndSet(0, 1); deadline.cancel(false); timer.shutdownNow() }
    }

    private fun withEvidence(name: String, body: () -> Unit) {
        val directory = File(context.filesDir, "hardware-evidence").apply { mkdirs() }
        jsonFile = File(directory, "$name.json")
        eventFile = File(directory, "$name.jsonl").apply { writeText("") }
        report = JSONObject().put("schema_version", 1).put("test", name).put("status", "running")
            .put("run_id", arguments.getString("hardwareRunId") ?: UUID.randomUUID().toString())
            .put("started_utc_ms", System.currentTimeMillis()).put("records", JSONArray())
            .put("records_retention", "last 256; complete event stream in sibling JSONL")
            .put("device", JSONObject().put("model", Build.MODEL).put("device", Build.DEVICE)
                .put("product", Build.PRODUCT).put("hardware", Build.HARDWARE).put("fingerprint", Build.FINGERPRINT)
                .put("sdk", Build.VERSION.SDK_INT).put("abis", JSONArray(Build.SUPPORTED_ABIS.toList()))
                .put("page_size_bytes", Os.sysconf(OsConstants._SC_PAGESIZE)).put("is_64_bit", Process.is64Bit())
                .put("application_id", context.packageName))
        checkpoint()
        var problem: Throwable? = null
        try {
            record("started")
            body()
            requireOffline()
        } catch (error: Throwable) {
            problem = error
        } finally {
            if (nativeUsed) try { NativeLlama.unload() } catch (cleanup: Throwable) {
                if (problem == null) problem = cleanup else problem.addSuppressed(cleanup)
            }
            report.put("status", if (problem == null) "passed" else "failed")
                .put("finished_utc_ms", System.currentTimeMillis())
            problem?.let { report.put("error", JSONObject().put("type", it.javaClass.name)
                .put("message", it.message ?: "").put("stack", it.stackTraceToString().take(16384))) }
            record("finished")
            checkpoint()
        }
        problem?.let { throw it }
    }

    private fun record(event: String, data: JSONObject = JSONObject()) {
        val memory = ActivityManager.MemoryInfo()
        (context.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager).getMemoryInfo(memory)
        val battery = context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val item = JSONObject().put("event", event).put("data", data)
            .put("utc_ms", System.currentTimeMillis()).put("elapsed_realtime_ms", SystemClock.elapsedRealtime())
            .put("thermal_status", power.currentThermalStatus)
            .put("memory_available_bytes", memory.availMem).put("system_low_memory", memory.lowMemory)
            .put("process_pss_kib", Debug.getPss())
        battery?.let {
            val temperature = it.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, Int.MIN_VALUE)
            if (temperature != Int.MIN_VALUE) item.put("battery_temperature_c", temperature / 10.0)
            item.put("battery_level", it.getIntExtra(BatteryManager.EXTRA_LEVEL, -1))
                .put("battery_scale", it.getIntExtra(BatteryManager.EXTRA_SCALE, -1))
                .put("battery_plugged", it.getIntExtra(BatteryManager.EXTRA_PLUGGED, -1))
        }
        FileOutputStream(eventFile, true).use { output ->
            output.write((item.toString() + "\n").toByteArray(Charsets.UTF_8))
            output.fd.sync()
        }
        val records = report.getJSONArray("records")
        records.put(item)
        if (records.length() > 256) records.remove(0)
        report.put("record_count", ++recordCount)
        if (SystemClock.elapsedRealtime() - lastCheckpoint > 3000L) checkpoint()
    }

    private fun checkpoint() {
        val file = AtomicFile(jsonFile)
        val output = file.startWrite()
        try {
            output.write(report.toString(2).toByteArray(Charsets.UTF_8))
            file.finishWrite(output)
        } catch (error: Throwable) { file.failWrite(output); throw error }
        lastCheckpoint = SystemClock.elapsedRealtime()
    }

    private fun integerArgument(name: String, fallback: Int, bounds: IntRange): Int {
        val value = arguments.getString(name)?.let {
            requireNotNull(it.toIntOrNull()) { "$name must be an integer." }
        } ?: fallback
        require(value in bounds) { "$name must be in ${bounds.first}..${bounds.last}." }
        return value
    }

    private fun sha256(file: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().buffered().use { input ->
            val buffer = ByteArray(1024 * 1024)
            while (true) { val count = input.read(buffer); if (count < 0) break; digest.update(buffer, 0, count) }
        }
        return digest.digest().joinToString("") { "%02x".format(it.toInt() and 0xff) }
    }

    private fun digest(bytes: ByteArray) = MessageDigest.getInstance("SHA-256").digest(bytes)
        .joinToString("") { "%02x".format(it.toInt() and 0xff) }
    private fun elapsedMs(start: Long) = nanosToMs(SystemClock.elapsedRealtimeNanos() - start)
    private fun nanosToMs(nanos: Long) = nanos / 1_000_000.0
}
