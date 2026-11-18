package org.lastphone.app.nativebridge

import android.content.Context
import android.util.Log

/** The single platform object passed into the embedded Python engine. No method opens a network connection. */
class NativePlatform(context: Context) : AutoCloseable {
    private val zimState = lazy { NativeZim(context.applicationContext) }
    private val embeddingState = lazy { NativeEmbeddings() }
    private val zim by zimState
    private val embeddings by embeddingState

    fun pdfRender(path: String, page: Int, longSide: Int) = NativePdf.render(path, page, longSide)
    fun pdfPageCount(path: String) = NativePdf.pageCount(path)
    fun pdfPageSizes(path: String) = NativePdf.pageSizes(path)

    fun zimMainPath(path: String) = zim.mainPath(path)
    fun zimHasFulltext(path: String) = zim.hasFulltext(path)
    fun zimEntryInfo(path: String, entry: String) = zim.entryInfo(path, entry)
    fun zimRead(path: String, entry: String) = zim.read(path, entry)
    fun zimSearch(path: String, query: String, limit: Int) = zim.search(path, query, limit)
    fun zimClose() { if (zimState.isInitialized()) zim.close() }

    fun semanticLoad(path: String, threads: Int) = embeddings.load(path, threads)
    fun semanticEmbed(ids: Array<LongArray>, masks: Array<LongArray>, types: Array<LongArray>?) = embeddings.embed(ids, masks, types)
    fun semanticClose() { if (embeddingState.isInitialized()) embeddings.close() }

    fun llmCapabilities(): String = NativeLlama.capabilities().also { Log.i("LastphoneNative", it) }
    fun llmMeasure(path: String, options: String) = NativeLlama.measure(path, options)
    fun llmLoad(path: String, options: String) = NativeLlama.load(path, options)
    fun llmGenerate(request: String, callback: NativeLlama.TokenCallback) = NativeLlama.generate(request, callback)
    fun llmRenderChat(messages: String, addAssistant: Boolean) = NativeLlama.renderChat(messages, addAssistant)
    fun llmChatTemplate() = NativeLlama.chatTemplate()
    fun llmCancel() = NativeLlama.cancel()
    fun llmUnload() = NativeLlama.unload()

    override fun close() {
        NativeLlama.unload()
        zimClose()
        semanticClose()
    }
}
