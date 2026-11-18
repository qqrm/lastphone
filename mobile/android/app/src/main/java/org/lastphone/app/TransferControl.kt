package org.lastphone.app

import java.io.InterruptedIOException
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

/** Tracks only explicit native imports; cancellation never reaches completed files. */
class TransferControl {
    private val generation = AtomicInteger()
    private val count = AtomicInteger()
    val active: Boolean get() = count.get() > 0

    fun begin(): Ticket {
        val ticket = Ticket(generation.get())
        count.incrementAndGet()
        return ticket
    }
    fun cancel() { generation.incrementAndGet() }

    inner class Ticket internal constructor(private val started: Int) : AutoCloseable {
        private val closed = AtomicBoolean()
        fun check() {
            if (generation.get() != started || Thread.currentThread().isInterrupted)
                throw InterruptedIOException("Import cancelled")
        }
        override fun close() { if (closed.compareAndSet(false, true)) count.decrementAndGet() }
    }
}
