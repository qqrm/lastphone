package org.lastphone.app.nativebridge

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.nio.LongBuffer
import kotlin.math.max
import kotlin.math.sqrt

/** BGE inference only. Tokenization, index files and RRF stay in the original Python retrieval pipeline. */
class NativeEmbeddings : AutoCloseable {
    private val environment = OrtEnvironment.getEnvironment().apply { setTelemetry(false) }
    private var session: OrtSession? = null
    private var modelPath: String? = null

    @Synchronized fun load(path: String, threads: Int): String {
        val file = File(path).canonicalFile
        require(file.isFile) { "Embedding model is unavailable." }
        if (session == null || modelPath != file.path) {
            close()
            OrtSession.SessionOptions().use { options ->
                options.setIntraOpNumThreads(threads.coerceIn(1, 8))
                options.setInterOpNumThreads(1)
                options.setExecutionMode(OrtSession.SessionOptions.ExecutionMode.SEQUENTIAL)
                session = environment.createSession(file.path, options)
            }
            modelPath = file.path
        }
        return JSONObject().put("inputs", JSONArray(session!!.inputNames.toList()))
            .put("provider", "CPUExecutionProvider").toString()
    }

    private fun tensor(rows: Array<LongArray>, columns: Int): OnnxTensor {
        require(rows.all { it.size == columns }) { "Embedding token rows must have equal lengths." }
        val values = LongArray(rows.size * columns)
        rows.forEachIndexed { i, row -> row.copyInto(values, i * columns) }
        return OnnxTensor.createTensor(environment, LongBuffer.wrap(values), longArrayOf(rows.size.toLong(), columns.toLong()))
    }

    @Synchronized fun embed(ids: Array<LongArray>, masks: Array<LongArray>, types: Array<LongArray>?): Array<FloatArray> {
        val active = checkNotNull(session) { "Embedding model has not been loaded." }
        require(ids.isNotEmpty() && ids.size <= 64) { "Embedding batch must contain 1–64 rows." }
        val columns = ids[0].size
        require(columns in 1..320 && masks.size == ids.size) { "Invalid embedding token shape." }
        val inputs = LinkedHashMap<String, OnnxTensor>()
        try {
            inputs["input_ids"] = tensor(ids, columns)
            inputs["attention_mask"] = tensor(masks, columns)
            if ("token_type_ids" in active.inputNames) {
                val tokenTypes = types ?: Array(ids.size) { LongArray(columns) }
                require(tokenTypes.size == ids.size) { "Invalid token-type shape." }
                inputs["token_type_ids"] = tensor(tokenTypes, columns)
            }
            active.run(inputs).use { output ->
                val values = output[0] as? OnnxTensor ?: error("Embedding output is not a tensor.")
                val shape = values.info.shape
                require(shape.size == 3 && shape[0] == ids.size.toLong() && shape[1] == columns.toLong() && shape[2] == 384L) {
                    "Expected BGE token embeddings with shape [batch, tokens, 384]."
                }
                val buffer = values.floatBuffer
                return Array(ids.size) { row ->
                    val vector = FloatArray(384)
                    buffer.position(row * columns * 384)
                    buffer.get(vector)
                    var squared = 0f
                    for (value in vector) squared += value * value
                    val norm = max(sqrt(squared), 1e-9f)
                    for (i in vector.indices) vector[i] /= norm
                    vector
                }
            }
        } finally { inputs.values.forEach { it.close() } }
    }

    @Synchronized override fun close() {
        session?.close()
        session = null
        modelPath = null
    }
}
