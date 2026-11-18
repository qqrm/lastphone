package org.lastphone.app

import java.io.InterruptedIOException
import org.junit.Assert.*
import org.junit.Test

class TransferControlTest {
    @Test fun cancellationStopsExistingCopiesAndANewUserImportCanRun() {
        val control = TransferControl()
        val first = control.begin()
        val second = control.begin()
        assertTrue(control.active)
        control.cancel()
        for (ticket in listOf(first, second)) {
            try { ticket.check(); fail("A cancelled import continued") }
            catch (_: InterruptedIOException) { }
        }
        val fresh = control.begin()
        fresh.check()
        first.close(); first.close(); second.close()
        assertTrue("Closing a ticket twice must not hide the current import", control.active)
        fresh.close()
        assertFalse(control.active)
    }
}
