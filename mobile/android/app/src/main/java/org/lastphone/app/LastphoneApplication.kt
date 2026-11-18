package org.lastphone.app

import android.app.Application

class LastphoneApplication : Application() {
    lateinit var engine: EngineHost
        private set

    override fun onCreate() {
        super.onCreate()
        engine = EngineHost(this)
    }
}
