package org.lastphone.app.nativebridge

import android.graphics.Bitmap
import android.graphics.Color
import android.graphics.Matrix
import android.graphics.pdf.PdfRenderer
import android.os.ParcelFileDescriptor
import org.json.JSONArray
import java.io.ByteArrayOutputStream
import java.io.File
import kotlin.math.max
import kotlin.math.roundToInt

/** Android's PDFium-backed renderer; page numbers and sizing match server.render_page. */
object NativePdf {
    private val lock = Any()

    private fun <T> withDocument(path: String, body: (PdfRenderer) -> T): T = synchronized(lock) {
        ParcelFileDescriptor.open(File(path), ParcelFileDescriptor.MODE_READ_ONLY).use { fd ->
            PdfRenderer(fd).use(body)
        }
    }

    @JvmStatic fun pageCount(path: String): Int = withDocument(path) { it.pageCount }

    @JvmStatic fun pageSizes(path: String): String = withDocument(path) { pdf ->
        val result = JSONArray()
        for (number in 0 until minOf(pdf.pageCount, 20_000)) {
            pdf.openPage(number).use { page ->
                result.put(JSONArray().put(page.width).put(page.height))
            }
        }
        result.toString()
    }

    @JvmStatic fun render(path: String, pageNumber: Int, longSide: Int): ByteArray = withDocument(path) { pdf ->
        require(pageNumber in 1..pdf.pageCount) { "PDF page is out of range." }
        pdf.openPage(pageNumber - 1).use { page ->
            val side = longSide.coerceIn(160, 1600)
            require(page.width > 0 && page.height > 0) { "PDF page has invalid dimensions." }
            val scale = side.toFloat() / max(page.width, page.height)
            val width = max(1, (page.width * scale).roundToInt())
            val height = max(1, (page.height * scale).roundToInt())
            val bitmap = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888)
            try {
                bitmap.eraseColor(Color.WHITE)
                val transform = Matrix().apply { setScale(scale, scale) }
                page.render(bitmap, null, transform, PdfRenderer.Page.RENDER_MODE_FOR_DISPLAY)
                ByteArrayOutputStream().use { output ->
                    check(bitmap.compress(Bitmap.CompressFormat.PNG, 100, output)) { "PDF PNG encoding failed." }
                    output.toByteArray()
                }
            } finally {
                bitmap.recycle()
            }
        }
    }
}
