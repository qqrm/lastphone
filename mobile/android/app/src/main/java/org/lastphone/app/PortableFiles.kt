package org.lastphone.app

import android.content.Context
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.os.StatFs
import android.os.storage.StorageManager
import android.provider.DocumentsContract
import android.provider.OpenableColumns
import java.io.File
import java.io.IOException
import java.util.UUID
import java.nio.file.Files

/** SAF is treated as a stream provider, never as an arbitrary filesystem path. */
class PortableFiles(private val context: Context) {
    companion object {
        private val copiesLock = Any()
        private var cleaned = false
        private val stagingName = Regex("\\.lastphone-[0-9a-f-]{36}\\.(pending|part)")
        fun safeName(name: String): String {
            require(name.isNotBlank() && name != "." && name != ".." &&
                name.none { it == '/' || it == '\\' || it == '\u0000' || it.code < 32 }) { "Unsafe file name" }
            require(name.toByteArray(Charsets.UTF_8).size <= 240) { "File name is too long" }
            return name
        }
        fun libraryRoot(folder: File): File? {
            fun valid(candidate: File) = File(candidate, "library-data/catalog.json").isFile &&
                File(candidate, "library-data/search.sqlite").isFile
            if (valid(folder)) return folder
            return folder.listFiles()?.filter { it.isDirectory }?.singleOrNull { valid(it) }
        }
    }

    private val journal get() = context.getSharedPreferences("lastphone-copy-staging", Context.MODE_PRIVATE)
    private fun remember(file: File, add: Boolean) = synchronized(copiesLock) {
        val paths = journal.getStringSet("paths", emptySet())!!.toMutableSet()
        if (add) paths.add(file.absolutePath) else paths.remove(file.absolutePath)
        check(journal.edit().putStringSet("paths", paths).commit()) { "Cannot save import recovery state" }
    }
    /** Only paths this app journaled before creating a staging file are removed. */
    fun cleanupInterruptedCopies() = synchronized(copiesLock) {
        if (cleaned) return@synchronized
        val parent = File(context.getExternalFilesDir(null) ?: context.filesDir, "libraries").canonicalFile
        val remaining = journal.getStringSet("paths", emptySet())!!.toMutableSet()
        for (path in remaining.toList()) {
            val file = File(path)
            if (!stagingName.matches(file.name)) continue
            val removed = if (file.name.endsWith(".pending")) {
                if (file.parentFile?.canonicalFile != parent) false
                else if (Files.isSymbolicLink(file.toPath())) file.delete()
                else file.deleteRecursively()
            } else !file.exists() || file.delete() // delete a file/link, never follow it.
            if (removed) remaining.remove(path)
        }
        journal.edit().putStringSet("paths", remaining).commit()
        cleaned = true
    }

    /** Only the OS external-storage provider and a known StorageVolume may yield an in-place path. */
    fun sharedDirectory(uri: Uri): File? {
        if (uri.authority != "com.android.externalstorage.documents") return null
        val id = DocumentsContract.getTreeDocumentId(uri)
        val volumeId = id.substringBefore(':')
        val relative = id.substringAfter(':', "")
        val parts = relative.split('/').filter { it.isNotEmpty() }
        try { parts.forEach(::safeName) } catch (_: IllegalArgumentException) { return null }
        val volume = if (Build.VERSION.SDK_INT >= 30) {
            context.getSystemService(StorageManager::class.java).storageVolumes
                .firstOrNull { if (volumeId == "primary") it.isPrimary else it.uuid.equals(volumeId, true) }?.directory
        } else if (volumeId == "primary") Environment.getExternalStorageDirectory() else null
        volume ?: return null
        val base = volume.canonicalFile
        val selected = File(base, parts.joinToString("/")).canonicalFile
        return selected.takeIf { it == base || it.path.startsWith(base.path + File.separator) }
    }

    private data class Entry(val id: String, val path: String, val size: Long, val directory: Boolean)
    private fun entries(tree: Uri, id: String, prefix: String, output: MutableList<Entry>, seen: MutableSet<String>, depth: Int, checkCancelled: () -> Unit) {
        checkCancelled()
        require(depth <= 64 && seen.add(id)) { "This folder contains a cycle or is too deeply nested" }
        val children = DocumentsContract.buildChildDocumentsUriUsingTree(tree, id)
        context.contentResolver.query(children, arrayOf(
            DocumentsContract.Document.COLUMN_DOCUMENT_ID, DocumentsContract.Document.COLUMN_DISPLAY_NAME,
            DocumentsContract.Document.COLUMN_MIME_TYPE, DocumentsContract.Document.COLUMN_SIZE
        ), null, null, null)?.use { cursor ->
            val names = mutableSetOf<String>()
            while (cursor.moveToNext()) {
                checkCancelled()
                check(output.size < 1_000_000) { "Too many files in this folder" }
                val childId = cursor.getString(0)
                val name = safeName(cursor.getString(1))
                check(names.add(name)) { "Duplicate file name: $name" }
                val path = if (prefix.isEmpty()) name else "$prefix/$name"
                val directory = cursor.getString(2) == DocumentsContract.Document.MIME_TYPE_DIR
                output += Entry(childId, path, if (cursor.isNull(3)) 0 else cursor.getLong(3).coerceAtLeast(0), directory)
                if (directory) entries(tree, childId, path, output, seen, depth + 1, checkCancelled)
            }
        } ?: throw IOException("The selected folder is no longer available")
    }

    /** Copy into a separate staging tree, then rename. The currently open library is never overwritten. */
    fun importTree(tree: Uri, requireLibrary: Boolean, checkCancelled: () -> Unit = {}, progress: (String) -> Unit): File {
        cleanupInterruptedCopies()
        val items = mutableListOf<Entry>()
        entries(tree, DocumentsContract.getTreeDocumentId(tree), "", items, mutableSetOf(), 0, checkCancelled)
        val parent = File(context.getExternalFilesDir(null) ?: context.filesDir, "libraries").apply { mkdirs() }
        val required = items.fold(0L) { size, item -> Math.addExact(size, item.size) }
        require(StatFs(parent.path).availableBytes >= required + 256L * 1024 * 1024) { "Not enough storage to copy this folder" }
        val id = UUID.randomUUID().toString()
        val staging = File(parent, ".lastphone-$id.pending")
        remember(staging, true)
        check(staging.mkdirs()) { "Cannot create import staging folder" }
        val target = File(parent, id)
        var copied = 0L
        try {
            items.forEachIndexed { index, item ->
                checkCancelled()
                if (Thread.currentThread().isInterrupted) throw IOException("Import cancelled")
                val out = File(staging, item.path)
                check(out.canonicalPath.startsWith(staging.canonicalPath + File.separator)) { "Path leaves the selected folder" }
                if (item.directory) { check(out.mkdirs() || out.isDirectory); return@forEachIndexed }
                out.parentFile?.mkdirs()
                val uri = DocumentsContract.buildDocumentUriUsingTree(tree, item.id)
                context.contentResolver.openInputStream(uri)?.use { input ->
                    out.outputStream().use { output ->
                        val buffer = ByteArray(1024 * 1024)
                        while (true) {
                            checkCancelled()
                            val count = input.read(buffer)
                            if (count < 0) break
                            checkCancelled()
                            if (Thread.currentThread().isInterrupted) throw IOException("Import cancelled")
                            if (StatFs(parent.path).availableBytes < count + 32L * 1024 * 1024) throw IOException("Storage is full")
                            output.write(buffer, 0, count)
                            copied += count
                        }
                        output.fd.sync()
                    }
                } ?: throw IOException("Cannot read ${item.path}")
                if (item.size > 0) require(out.length() == item.size) { "File changed during import: ${item.path}" }
                progress("${index + 1} / ${items.size} files · ${copied / 1_000_000} MB")
            }
            if (requireLibrary) require(libraryRoot(staging) != null) { "Select a folder containing library-data/catalog.json and search.sqlite" }
            checkCancelled()
            check(staging.renameTo(target)) { "Cannot activate the copied folder" }
            remember(staging, false)
            return libraryRoot(target) ?: target
        } catch (failure: Throwable) { if (staging.deleteRecursively()) remember(staging, false); throw failure }
    }

    fun importFile(uri: Uri, parent: File, checkCancelled: () -> Unit = {}, progress: (String) -> Unit): File {
        cleanupInterruptedCopies()
        checkCancelled()
        var name = "selected-file"
        context.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use {
            if (it.moveToFirst()) name = safeName(it.getString(0))
        }
        parent.mkdirs()
        val destination = File(parent, name)
        require(!destination.exists()) { "A file named $name already exists. Choose it in model settings, or use another file name." }
        val temporary = File(parent, ".lastphone-${UUID.randomUUID()}.part")
        remember(temporary, true)
        try {
            context.contentResolver.openInputStream(uri)?.use { input ->
                temporary.outputStream().use { output ->
                    val buffer = ByteArray(1024 * 1024)
                    var total = 0L
                    while (true) {
                        checkCancelled()
                        val n = input.read(buffer)
                        if (n < 0) break
                        checkCancelled()
                        if (StatFs(parent.path).availableBytes < n + 64L * 1024 * 1024) throw IOException("Not enough storage")
                        output.write(buffer, 0, n); total += n
                        progress("${total / 1_000_000} MB copied")
                    }
                    output.fd.sync()
                }
            } ?: throw IOException("Cannot read the selected file")
            checkCancelled()
            synchronized(copiesLock) {
                require(!destination.exists()) { "A file named $name already exists" }
                check(temporary.renameTo(destination)) { "Cannot save the selected file" }
                remember(temporary, false)
            }
            return destination
        } catch (failure: Throwable) { if (temporary.delete() || !temporary.exists()) remember(temporary, false); throw failure }
    }
}
