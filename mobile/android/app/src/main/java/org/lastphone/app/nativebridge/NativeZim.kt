package org.lastphone.app.nativebridge

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import org.kiwix.libkiwix.JNIKiwix
import org.kiwix.libzim.Archive
import org.kiwix.libzim.Query
import org.kiwix.libzim.Searcher
import java.io.File

/** Thin libzim adapter. URL routing, file authorization and HTML policy remain in the Python engine. */
class NativeZim(context: Context) : AutoCloseable {
    private data class OpenArchive(val file: File, val size: Long, val modified: Long, val archive: Archive)
    private val archives = LinkedHashMap<String, OpenArchive>(4, 0.75f, true)
    private val kiwix = JNIKiwix(context.applicationContext).also {
        val data = File(context.filesDir, "kiwix").apply { mkdirs() }
        it.setDataDirectory(data.absolutePath)
    }

    private fun archive(path: String): Archive {
        val file = File(path).canonicalFile
        require(file.isFile) { "Encyclopedia file is unavailable." }
        val previous = archives[file.path]
        if (previous != null && previous.size == file.length() && previous.modified == file.lastModified()) {
            return previous.archive
        }
        previous?.archive?.dispose()
        archives.remove(file.path)
        if (archives.size >= 4) {
            val oldest = archives.entries.iterator()
            val value = oldest.next().value
            oldest.remove()
            value.archive.dispose()
        }
        val opened = Archive(file.path)
        archives[file.path] = OpenArchive(file, file.length(), file.lastModified(), opened)
        return opened
    }

    @Synchronized fun mainPath(path: String): String = archive(path).mainEntry.getItem(true).path
    @Synchronized fun hasFulltext(path: String): Boolean = archive(path).hasFulltextIndex()

    @Synchronized fun entryInfo(path: String, entryPath: String): String {
        val entry = archive(path).getEntryByPath(entryPath)
        val item = entry.getItem(true)
        return JSONObject().put("title", entry.title).put("path", item.path)
            .put("mimetype", item.mimetype).put("size", item.size).toString()
    }

    @Synchronized fun read(path: String, entryPath: String): ByteArray {
        val item = archive(path).getEntryByPath(entryPath).getItem(true)
        require(item.size <= 64L * 1024 * 1024) { "Encyclopedia entry exceeds the inline reader limit." }
        val data = item.data
        return try { data.data } finally { data.dispose() }
    }

    @Synchronized fun search(path: String, text: String, limit: Int): String {
        val result = JSONArray()
        val archive = archive(path)
        if (!archive.hasFulltextIndex()) return result.toString()
        val query = Query(text)
        val searcher = Searcher(archive)
        try {
            val search = searcher.search(query)
            try {
                val iterator = search.getResults(0, limit.coerceIn(0, 100))
                try {
                    while (iterator.hasNext() && result.length() < limit) {
                        val entry = iterator.next()
                        result.put(JSONObject().put("title", entry.title).put("path", entry.path))
                    }
                } finally { iterator.dispose() }
            } finally { search.dispose() }
        } finally {
            searcher.dispose()
            query.dispose()
        }
        return result.toString()
    }

    @Synchronized override fun close() {
        archives.values.forEach { it.archive.dispose() }
        archives.clear()
    }
}
