package org.lastphone.app.nativebridge

import android.graphics.BitmapFactory
import android.graphics.Color
import android.graphics.Paint
import android.graphics.pdf.PdfDocument
import android.util.Log
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/** These tests call real Android/native libraries, including an actual GGUF decode. */
@RunWith(AndroidJUnit4::class)
class NativeAdaptersTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context = instrumentation.targetContext
    private val directory = File(context.cacheDir, "native-adapter-test").apply { mkdirs() }

    private fun asset(name: String): File {
        val file = File(directory, name.substringAfterLast('/'))
        instrumentation.context.assets.open(name).use { input -> file.outputStream().use { input.copyTo(it) } }
        return file
    }

    @After fun release() {
        NativeLlama.unload()
        directory.deleteRecursively()
    }

    @Test fun pdfUsesRealPageGeometryAndRenderer() {
        val file = File(directory, "pages.pdf")
        val pdf = PdfDocument()
        try {
            for ((number, dimensions) in listOf(300 to 600, 600 to 300).withIndex()) {
                val page = pdf.startPage(PdfDocument.PageInfo.Builder(dimensions.first, dimensions.second, number + 1).create())
                page.canvas.drawColor(Color.WHITE)
                page.canvas.drawRect(10f, 10f, 200f, 200f, Paint().apply { color = Color.BLACK })
                pdf.finishPage(page)
            }
            file.outputStream().use { pdf.writeTo(it) }
        } finally { pdf.close() }
        assertEquals(2, NativePdf.pageCount(file.path))
        val sizes = JSONArray(NativePdf.pageSizes(file.path))
        assertEquals(300, sizes.getJSONArray(0).getInt(0))
        assertEquals(600, sizes.getJSONArray(0).getInt(1))
        val bytes = NativePdf.render(file.path, 2, 800)
        val bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
        assertNotNull(bitmap)
        try {
            assertEquals(800, bitmap.width)
            assertEquals(400, bitmap.height)
            assertEquals(Color.BLACK, bitmap.getPixel(50, 50))
        } finally { bitmap.recycle() }
        try {
            NativePdf.render(file.path, 0, 800)
            fail("A zero-based PDF page was accepted.")
        } catch (_: IllegalArgumentException) { }
    }

    private fun loadCpu(): JSONObject {
        val model = asset("models/stories260K.gguf")
        val options = JSONObject().put("backend", "cpu").put("n_ctx", 256).put("n_threads", 2)
            .put("n_batch", 64).put("n_ubatch", 64).toString()
        val measured = JSONObject(NativeLlama.measure(model.path, options))
        assertEquals(measured.toString(), "fit", measured.getString("ev"))
        assertTrue(measured.getBoolean("cpu_only"))
        val loaded = JSONObject(NativeLlama.load(model.path, options))
        assertEquals(loaded.toString(), "ready", loaded.getString("ev"))
        assertEquals("cpu", loaded.getString("backend"))
        assertEquals(0, loaded.getInt("n_gpu_layers"))
        return loaded
    }

    private fun request(text: String = "Once upon a time", count: Int = 12): String = JSONObject()
        .put("id", 1)
        .put("segments", JSONArray().put(JSONArray().put(text).put(false)))
        .put("max_tokens", count).put("temp", 0.0).put("seed", 1).toString()

    @Test fun cpuDecodesPinnedGgufAndReusesThePrompt() {
        loadCpu()
        val text = StringBuilder()
        val first = JSONObject(NativeLlama.generate(request()) { event ->
            val token = JSONObject(event)
            assertEquals("tok", token.getString("ev"))
            text.append(token.getString("t"))
        })
        assertEquals(first.toString(), "done", first.getString("ev"))
        assertTrue(first.getInt("n_gen") > 0)
        assertTrue(text.isNotBlank())
        val second = JSONObject(NativeLlama.generate(request()) { })
        assertEquals(second.toString(), "done", second.getString("ev"))
        assertTrue(second.getInt("n_reused") > 0)
        // This fixture's BOS is token 1, "<s>". A model-provided template may
        // already include it, unlike the original Lastbook Gemma renderer.
        val withBos = JSONObject(request()).put("segments", JSONArray()
            .put(JSONArray().put("<s>").put(true))
            .put(JSONArray().put("Once upon a time").put(false)))
        val templated = JSONObject(NativeLlama.generate(withBos.toString()) { })
        assertEquals(templated.toString(), "done", templated.getString("ev"))
        assertEquals("A template BOS must not be added twice.", first.getInt("n_prompt"), templated.getInt("n_prompt"))
    }

    @Test fun cancellationStopsNativeDecodeAndTheNextRequestStillWorks() {
        loadCpu()
        var pieces = 0
        val stopped = JSONObject(NativeLlama.generate(request(count = 64)) {
            pieces++
            NativeLlama.cancel()
        })
        assertEquals(stopped.toString(), "cancel", stopped.getString("reason"))
        assertEquals(1, pieces)
        val next = JSONObject(NativeLlama.generate(request("The child saw a 🐈 and said привет.", 4)) { })
        assertEquals(next.toString(), "done", next.getString("ev"))
        assertTrue(next.getInt("n_gen") > 0)
    }

    @Test fun vulkanSelectionUsesRealCapabilitiesAndCanReturnToCpu() {
        val capabilities = JSONObject(NativeLlama.capabilities())
        assertTrue(capabilities.toString(), capabilities.getBoolean("cpu"))
        val model = asset("models/stories260K.gguf")
        val options = JSONObject().put("backend", "vulkan").put("n_ctx", 256).put("n_threads", 2)
            .put("n_batch", 64).put("n_ubatch", 64).toString()
        val loaded = JSONObject(NativeLlama.load(model.path, options))
        if (capabilities.getBoolean("vulkan")) {
            assertEquals(loaded.toString(), "ready", loaded.getString("ev"))
            assertEquals("vulkan", loaded.getString("backend"))
            assertNotEquals("The tiny model should fit at least one GPU layer.", 0, loaded.getInt("n_gpu_layers"))
            val generated = JSONObject(NativeLlama.generate(request(count = 4)) { })
            assertEquals(generated.toString(), "done", generated.getString("ev"))
            assertTrue(generated.getInt("n_gen") > 0)
            assertEquals("vulkan", generated.getString("backend"))
            Log.i("LastphoneNativeTest", "Executed real Vulkan decode: $capabilities")
        } else {
            assertEquals(loaded.toString(), "error", loaded.getString("ev"))
            assertTrue(loaded.getString("msg").contains("Vulkan inference is unavailable"))
            Log.i("LastphoneNativeTest", "No Vulkan device; verified explicit error and CPU fallback: $capabilities")
        }
        loadCpu()
        val cpu = JSONObject(NativeLlama.generate(request(count = 4)) { })
        assertEquals(cpu.toString(), "done", cpu.getString("ev"))
        assertEquals("cpu", cpu.getString("backend"))
        assertTrue(cpu.getInt("n_gen") > 0)
    }

    @Test fun invalidGgufReturnsARealLoadError() {
        val invalid = File(directory, "broken.gguf").apply { writeText("GGUFbroken") }
        val response = JSONObject(NativeLlama.load(invalid.path, "{\"backend\":\"cpu\",\"n_ctx\":256}"))
        assertEquals(response.toString(), "error", response.getString("ev"))
        val absent = JSONObject(NativeLlama.generate(request()) { })
        assertEquals("not_loaded", absent.getString("code"))
    }

    @Test fun zimReadsARealArchiveAndResolvesItsMainEntry() {
        val file = asset("collections/small.zim")
        NativeZim(context).use { reader ->
            val path = reader.mainPath(file.path)
            assertTrue(path.isNotEmpty())
            val info = JSONObject(reader.entryInfo(file.path, path))
            assertTrue(info.getString("mimetype").isNotEmpty())
            val content = reader.read(file.path, path)
            assertTrue(content.isNotEmpty())
            assertEquals(info.getLong("size"), content.size.toLong())
            val hits = JSONArray(reader.search(file.path, "test", 8))
            assertTrue(hits.length() <= 8)
            if (!reader.hasFulltext(file.path)) assertEquals(0, hits.length())
        }
    }
}
