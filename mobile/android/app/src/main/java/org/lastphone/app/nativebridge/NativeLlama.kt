package org.lastphone.app.nativebridge

/** JSON records match the upstream runner. The native layer serializes model access; cancel stays concurrent. */
object NativeLlama {
    init { System.loadLibrary("lastphone_llm") }

    fun interface TokenCallback { fun onEvent(eventJson: String) }

    @JvmStatic external fun capabilities(): String
    @JvmStatic external fun measure(path: String, optionsJson: String): String
    @JvmStatic external fun load(path: String, optionsJson: String): String
    @JvmStatic external fun generate(requestJson: String, callback: TokenCallback): String
    @JvmStatic external fun renderChat(messagesJson: String, addAssistant: Boolean): String
    @JvmStatic external fun chatTemplate(): String
    @JvmStatic external fun cancel()
    @JvmStatic external fun unload()
}
