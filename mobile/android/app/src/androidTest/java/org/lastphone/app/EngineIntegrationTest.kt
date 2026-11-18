package org.lastphone.app

import android.Manifest
import android.content.Context
import android.graphics.Bitmap
import android.os.Build
import android.util.Base64
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.rule.GrantPermissionRule
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.Rule
import org.junit.rules.TestName
import org.junit.runner.RunWith
import java.io.File
import java.io.ByteArrayInputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.UUID
import java.util.zip.GZIPInputStream
import com.chaquo.python.Python

/** Actual APK -> WebView -> Chaquopy -> unchanged Lastbook core integration. */
@RunWith(AndroidJUnit4::class)
class EngineIntegrationTest {
    @get:Rule val testName = TestName()
    @get:Rule val notificationPermission: GrantPermissionRule = GrantPermissionRule.grant(
        *if (Build.VERSION.SDK_INT >= 33) arrayOf(Manifest.permission.POST_NOTIFICATIONS) else emptyArray()
    )
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
    @After fun closeScreen() {
        try {
            val instrumentation = InstrumentationRegistry.getInstrumentation()
            val evidence = File(instrumentation.targetContext.filesDir, "test-evidence").apply { mkdirs() }
            instrumentation.uiAutomation.takeScreenshot()?.let { bitmap ->
                File(evidence, "${testName.methodName}.png").outputStream().use {
                    bitmap.compress(Bitmap.CompressFormat.PNG, 100, it)
                }
                bitmap.recycle()
            }
        } finally {
            if (::scenario.isInitialized) scenario.close()
        }
    }

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
        val cues = JSONObject(String(transcript)).getJSONArray("cues")
        assertEquals(2, cues.length())
        assertEquals(12, cues.getJSONObject(1).getInt("t"))
        js("location.hash='#/watch/fixture-video'; true")
        awaitJs("document.querySelector('video') !== null && document.querySelector('video').readyState >= 1", 45)
        awaitJs("document.querySelector('.cue[data-t=\"12\"]') !== null")
        js("document.querySelector('.cue[data-t=\"12\"]').click(); true")
        awaitJs("document.querySelector('video').currentTime >= 11.9")
        js("document.querySelector('video').pause(); true")
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
            # Echo the exact id through both native return paths, independently
            # of the tiny model's output language or token boundaries.
            from lastphone_llm import _callback
            unicode_id = 'Київ · Привет · 漢字 · 😀\x00tail'
            unicode_events = []
            unicode_request = json.dumps({
                'id': unicode_id, 'segments': [['Once upon a time', False]],
                'max_tokens': 2, 'temp': 0, 'seed': 1, 'stop': [],
            }, ensure_ascii=False)
            assert 'Київ' in unicode_request and '😀' in unicode_request
            unicode_done = json.loads(str(engine.native.llmGenerate(
                unicode_request, _callback(lambda raw: unicode_events.append(json.loads(raw))))))
            assert unicode_events, 'No native events received for the Unicode round trip'
            assert all(event.get('id') == unicode_id for event in unicode_events), unicode_events
            assert unicode_done.get('ev') == 'done', unicode_done
            assert unicode_done.get('id') == unicode_id, unicode_done
            engine.local.stop('test completed')
        """.trimIndent()
        builtins.callAttr("exec", script, scope, scope)
    }

    @Test fun pythonSemanticEmbedderCrossesJniAndRunsActualOnnx() {
        // Same valid graph as NativeEmbeddingsTest; the generator is retained in
        // androidTest/fixtures/make_cls_fixture.py. Its CLS output is
        // input_ids + attention_mask + 2 * token_type_ids + [3, 4, 0, ...].
        val graph = "H4sIAAAAAAACA+1VzU7CQBBuKdjtBBDXg2iMgZqYiOHkjRPiVY9euDQLu4SGsq3sNKIn3sEX8Pl8CodW/AmGF7DfZpOdnW/2m9nLxxhv3wmDyTTWqhVqg4t0rjQKDGPdmoRLTBeq974PbahjPFM6wOdEBaE04OBTzCvrUPrOfRqBD16okxSzbJ7gHp2zEuM7N1LCJXzfQF0gkhYpBXNhZtwVUbQuzqk+bGK+N4ligcYv31KrHQ9KGDftN7sE5/CZgrJYhoYztUyEltSR96DNY6rUiyLNr2uojIQh3kFEDwXTUEqayNC0KtPkJ9HmL7rjyHSFFAmqRRcVyULD7dmlwXrsTo3ZeZTJdl6rzKa1chq2v6paVr9vWSvaBQoUKFDgP2GQm8zw4ocf8mM4Yi4/BJdTFsdTYHwvM1QzvNoywt3k3z68gzy6hm2j42dwymze/IMPDlnYoAxWo/YBpTVuNBgIAAA="
        val modelDirectory = File(root, "derived/models/bge-small-en-v1.5").apply { mkdirs() }
        val model = File(modelDirectory, "model.onnx")
        val tokenizer = File(modelDirectory, "tokenizer.json")
        try {
            GZIPInputStream(ByteArrayInputStream(Base64.decode(graph, Base64.DEFAULT))).use { input ->
                model.outputStream().use { input.copyTo(it) }
            }
            copy(InstrumentationRegistry.getInstrumentation().context, "bge-tokenizer.json", tokenizer)
            host.call("open_library", root.absolutePath)
            val builtins = Python.getInstance().getModule("builtins")
            val scope = builtins.callAttr("dict")
            scope.callAttr("__setitem__", "engine", host.engine())
            val script = """
                import numpy as np
                index = engine.server.INDEX
                assert index.emb is not None, index.status
                assert index.emb.tok.vocab['[CLS]'] == 101
                vectors = index.emb.embed(['water', 'Привет, café 中文 and a longer passage', ''])
                assert vectors.shape == (3, 384), vectors.shape
                assert vectors.dtype == np.float32, vectors.dtype
                expected = np.full(384, 102, dtype=np.float32)
                expected[0] += 3
                expected[1] += 4
                expected /= np.linalg.norm(expected)
                for vector in vectors:
                    np.testing.assert_allclose(vector, expected, rtol=1e-5, atol=1e-6)
                np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), np.ones(3), atol=1e-5)
                assert index.emb.embed([]).shape == (0, 384)
            """.trimIndent()
            builtins.callAttr("exec", script, scope, scope)
        } finally {
            model.delete()
            tokenizer.delete()
            host.call("open_library", root.absolutePath)
        }
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
