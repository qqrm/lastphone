package org.lastphone.app

import org.junit.Assert.*
import org.junit.Test
import java.io.File
import java.nio.file.Files

class PortableFilesTest {
    @Test fun unsafeProviderNamesAreRefusedBeforeWriting() {
        listOf("", ".", "..", "../escape", "x/y", "x\\y", "x\u0000y", "x\ny").forEach { name ->
            try { PortableFiles.safeName(name); fail("Accepted $name") } catch (_: IllegalArgumentException) { }
        }
        assertEquals("Вода и поход.pdf", PortableFiles.safeName("Вода и поход.pdf"))
    }
    @Test fun findsTheActualLibraryAndRejectsAnAmbiguousParent() {
        val temp = Files.createTempDirectory("lastphone-folders").toFile()
        try {
            fun library(name: String) = File(temp, name).apply {
                File(this, "library-data").mkdirs()
                File(this, "library-data/catalog.json").writeText("[]")
                File(this, "library-data/search.sqlite").writeBytes(byteArrayOf())
            }
            val first = library("LastbookData")
            assertEquals(first, PortableFiles.libraryRoot(temp))
            library("AnotherLibrary")
            assertNull(PortableFiles.libraryRoot(temp))
            assertEquals(first, PortableFiles.libraryRoot(first))
        } finally { temp.deleteRecursively() }
    }
}
