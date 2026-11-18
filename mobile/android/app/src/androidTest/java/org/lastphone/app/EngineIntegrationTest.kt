package org.lastphone.app

import android.content.Context
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.UUID
import com.chaquo.python.Python

/** Actual APK -> WebView -> Chaquopy -> unchanged Lastbook core integration. */
@RunWith(AndroidJUnit4::class)
class EngineIntegrationTest {
    private lateinit var host: EngineHost
    private lateinit var scenario: ActivityScenario<MainActivity>
    private lateinit var root: File

    @Before fun openFixture() {
        val application = ApplicationProvider.getApplicationContext<LastphoneApplication>()
        host = application.engine
        root = File(application.getExternalFilesDir(null), "integration-fixture-${UUID.randomUUID()}")
        copy(InstrumentationRegistry.getInstrumentation().context, "library", root)
        host.preferences.edit().putString("data_root", root.absolutePath).commit()
        host.start().get(180, TimeUnit.SECONDS)
        host.call("open_library", root.absolutePath)
        scenario = ActivityScenario.launch(MainActivity::class.java)
        awaitJs("document.readyState === 'complete' && document.querySelector('#main') !== null")
    }
    @After fun closeScreen() { scenario.close() }

    private fun copy(context: Context, path: String, output: File) {
        val children = context.assets.list(path) ?: emptyArray()
        if (children.isEmpty()) {
            output.parentFile?.mkdirs()
            context.assets.open(path).use { input -> output.outputStream().use { input.copyTo(it) } }
        } else { output.mkdirs(); children.forEach { copy(context, "$path/$it", File(output, it)) } }
    }
    private fun get(path: String): Pair<Int, ByteArray> = host.request("GET", EngineHost.ORIGIN + path).use { Pair(it.status, it.bytes()) }
    private fun js(expression: String): String {
        val done = CountDownLatch(1); var result = ""
        scenario.onActivity { activity -> activity.web.evaluateJavascript(expression) { result = it; done.countDown() } }
        assertTrue("JavaScript callback timed out: $expression", done.await(10, TimeUnit.SECONDS))
        return result
    }
    private fun awaitJs(expression: String, timeoutSeconds: Int = 30) {
        val end = System.nanoTime() + TimeUnit.SECONDS.toNanos(timeoutSeconds.toLong())
        var last = ""
        while (System.nanoTime() < end) {
            last = js(expression)
            if (last == "true") return
            Thread.sleep(100)
        }
        fail("WebView condition failed: $expression; last=$last; page=${js("document.body.innerText.slice(0,1500)")}")
    }

    @Test fun pythonHasRealFts5AndFindsCyrillic() {
        val (code, bytes) = get("/api/find?q=%D0%B2%D0%BE%D0%B4%D0%B0")
        assertEquals(200, code)
        assertTrue(String(bytes).contains("fixture-html"))
        assertTrue(String(get("/api/doc/fixture-pdf").second).contains("Water test manual"))
        assertTrue(String(get("/api/page/fixture-pdf/1").second).contains("clean water"))
    }

    @Test fun pdfIsRenderedByAndroidAndNeverExecuted() {
        val (code, png) = get("/api/page/fixture-pdf/1.png?w=320")
        assertEquals(200, code)
        assertTrue(png.size > 300)
        assertArrayEquals(byteArrayOf(0x89.toByte(), 80, 78, 71, 13, 10, 26, 10), png.copyOf(8))
        val (documentCode, viewer) = get("/documents/fixture.pdf")
        assertEquals(200, documentCode)
        assertTrue(String(viewer).contains("pdf-page=1"))
        assertFalse(String(viewer).startsWith("%PDF"))
        assertTrue(get("/api/page/fixture-pdf/999.png").first in listOf(400, 404, 500))
    }

    @Test fun realMapGeometryAnd3dBuildingsSurviveThePort() {
        val (code, tile) = get("/api/map/tile/14/8192/8192")
        assertEquals(200, code)
        val map = JSONObject(String(tile))
        assertEquals("Fixture Road", map.getJSONObject("layers").getJSONArray("road").getJSONArray(0).getString(1))
        val (buildingCode, buildings) = get("/api/map/bldg3d/8192/8192")
        assertEquals(200, buildingCode)
        assertEquals(44, buildings.size)
        assertEquals(10, ByteBuffer.wrap(buildings).order(ByteOrder.LITTLE_ENDIAN).int)
    }

    @Test fun videoRangesAndTimedCaptionsRemainExact() {
        val video = File(root, "library-data/videos/fixture.mp4").readBytes()
        host.request("GET", EngineHost.ORIGIN + "/videos/fixture.mp4", mapOf("Range" to "bytes=31-160")).use { response ->
            assertEquals(206, response.status)
            assertArrayEquals(video.copyOfRange(31, 161), response.bytes())
            assertTrue(response.headers.values.any { it == "bytes 31-160/${video.size}" })
        }
        val (status, transcript) = get("/api/transcript/fixture-video")
        assertEquals(200, status)
        assertTrue(String(transcript).contains("Second caption for seeking"))
        js("location.hash='#/watch/fixture-video'; true")
        awaitJs("document.querySelector('video') !== null && document.querySelector('video').readyState >= 1", 45)
        js("document.querySelector('video').currentTime=1; true")
        awaitJs("document.querySelector('video').currentTime >= 0.9")
    }

    @Test fun javascriptPostsUseTheActualAuthenticatedToolLoop() {
        js("""window.__tool=null; fetch('/api/tool/search_library',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:'water'})}).then(async r=>window.__tool={status:r.status,body:await r.text()}).catch(e=>window.__tool={error:String(e)}); true""")
        awaitJs("window.__tool !== null")
        assertEquals("200", js("window.__tool.status"))
        assertTrue(js("window.__tool.body").contains("fixture-pdf"))
        // Bypassing the top-frame secret must fail even with the correct origin.
        host.request("POST", EngineHost.ORIGIN + "/api/tool/search_library", mapOf("Content-Type" to "application/json", "Origin" to EngineHost.ORIGIN), "{}".toByteArray()).use {
            assertEquals(403, it.status)
        }
    }

    @Test fun streamingUnavailableModelEndsCleanlyWithoutHanging() {
        host.call("set_model_settings", "{\"enabled\":false}")
        js("""window.__chat=null;fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({messages:[{role:'user',content:'water'}]})}).then(async r=>window.__chat={status:r.status,body:await r.text()}).catch(e=>window.__chat={error:String(e)});true""")
        awaitJs("window.__chat !== null")
        assertEquals("200", js("window.__chat.status"))
        assertTrue(js("window.__chat.body").contains("model_unavailable"))
    }

    @Test fun pythonAssistantReceivesRealJniTokens() {
        val model = File(root, "models/stories260K.gguf")
        copy(InstrumentationRegistry.getInstrumentation().context, "models/stories260K.gguf", model)
        val builtins = Python.getInstance().getModule("builtins")
        val scope = builtins.callAttr("dict")
        scope.callAttr("__setitem__", "engine", host.engine())
        scope.callAttr("__setitem__", "model_path", model.absolutePath)
        val script = """
            import json, time
            engine.set_model_settings(json.dumps({'path': model_path, 'mode':'gguf', 'enabled': True, 'backend': 'cpu', 'context': 2048, 'threads': 2}))
            deadline = time.monotonic() + 15
            while engine.local.state == 'checking' and time.monotonic() < deadline:
                time.sleep(.02)
            error = engine.local.ensure_running()
            assert error is None, error
            pieces = []
            result = engine.local.generate([['Once upon a time', False]], 24, lambda: False, pieces.append)
            assert result.get('n_gen', 0) > 0, result
            assert ''.join(pieces).strip(), 'JNI tokens were lost crossing the Python callback'
            engine.local.stop('test completed')
        """.trimIndent()
        builtins.callAttr("exec", script, scope, scope)
    }

    @Test fun untrustedArticleCannotRunScriptsOrReachTheNativeBridge() {
        host.request("GET", EngineHost.ORIGIN + "/pages/fixture.html").use { response ->
            assertEquals(200, response.status)
            assertTrue(response.headers.entries.any { it.key.equals("content-security-policy", true) && it.value.contains("sandbox") })
        }
        js("window.__untrustedExecuted=false; location.hash='#/read/fixture-html?view=web'; true")
        awaitJs("document.querySelector('#docbody iframe') !== null")
        assertEquals("false", js("window.__untrustedExecuted"))
        js("window.__foreign=null;fetch('https://example.invalid/leak').then(()=>window.__foreign='allowed').catch(()=>window.__foreign='blocked');true")
        awaitJs("window.__foreign !== null")
        assertEquals("\"blocked\"", js("window.__foreign"))
        for (path in listOf("/../settings/setup.json", "/documents/%2e%2e/%2e%2e/settings/setup.json", "/ui/../../engine/setup/roots.pem")) {
            assertTrue("Refuse $path", get(path).first in listOf(400, 403, 404))
        }
    }

    @Test fun activityRecreationPreservesHistoryAndWorksOffline() {
        js("localStorage.setItem('lastphone-test-bookmark','fixture-pdf:1');true")
        scenario.recreate()
        awaitJs("document.readyState === 'complete' && document.querySelector('#main') !== null")
        assertEquals("\"fixture-pdf:1\"", js("localStorage.getItem('lastphone-test-bookmark')"))
        js("location.hash='#/search?q=water';true")
        awaitJs("document.body.innerText.includes('Water test manual')")
    }
}
