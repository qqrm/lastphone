package org.lastphone.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import org.json.JSONObject

/** Exists only while a user-started operation is active; no boot receiver or periodic network work. */
class WorkService : Service() {
    private val handler = Handler(Looper.getMainLooper())
    private var wake: PowerManager.WakeLock? = null
    private var idle = 0
    private var stopped = false
    private val host get() = (application as LastphoneApplication).engine
    private val tick = object : Runnable {
        override fun run() {
            if (stopped) return
            host.worker.execute {
                try {
                    val state = JSONObject(host.call("work_state").toString())
                    handler.post {
                        if (stopped) return@post
                        val running = state.optBoolean("running") || state.optBoolean("chat") || state.optBoolean("setup") || state.optBoolean("lan")
                        idle = if (running) 0 else idle + 1
                        if (idle >= 3) { stopSelf(); return@post }
                        if (running && wake?.isHeld != true) wake?.acquire(10 * 60 * 1000L)
                        val label = when {
                            state.optBoolean("setup") -> "Preparing the offline library"
                            state.optBoolean("chat") -> "Generating an answer on this device"
                            state.optBoolean("lan") -> "Sharing the library on Wi-Fi"
                            else -> "Finishing local work"
                        }
                        val notification = notification(label)
                        if (Build.VERSION.SDK_INT >= 34) startForeground(1, notification,
                            if (state.optBoolean("setup")) ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC else ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
                        else startForeground(1, notification)
                        handler.postDelayed(this, 3000)
                    }
                } catch (_: Throwable) { handler.post { stopSelf() } }
            }
        }
    }
    override fun onCreate() {
        super.onCreate()
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel("local-work", "Local work", NotificationManager.IMPORTANCE_LOW)
        )
        wake = getSystemService(PowerManager::class.java).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "Lastphone:local-work").apply { setReferenceCounted(false) }
    }
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (Build.VERSION.SDK_INT >= 34) startForeground(1, notification("Starting local work"), ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        else startForeground(1, notification("Starting local work"))
        if (intent?.action == "STOP") {
            host.worker.execute { try { host.call("cancel_work") } finally { handler.post { stopSelf() } } }
        } else {
            idle = 0
            handler.removeCallbacks(tick)
            handler.postDelayed(tick, 1500)
        }
        return START_NOT_STICKY
    }
    private fun notification(message: String): Notification {
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val stop = PendingIntent.getService(this, 1, Intent(this, WorkService::class.java).setAction("STOP"), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        return Notification.Builder(this, "local-work").setSmallIcon(R.drawable.ic_lastphone)
            .setContentTitle("Lastphone").setContentText(message).setContentIntent(open).setOngoing(true)
            .addAction(Notification.Action.Builder(null, "Stop", stop).build()).build()
    }
    override fun onTimeout(startId: Int, fgsType: Int) {
        host.worker.execute { try { host.call("cancel_work") } finally { handler.post { stopSelf() } } }
    }
    override fun onDestroy() {
        stopped = true; handler.removeCallbacks(tick)
        if (wake?.isHeld == true) wake?.release()
        super.onDestroy()
    }
    override fun onBind(intent: Intent?): IBinder? = null
}
