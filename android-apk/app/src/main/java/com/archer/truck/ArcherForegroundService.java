package com.archer.truck;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import androidx.core.app.NotificationCompat;
import org.json.JSONObject;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * Foreground service that keeps a persistent live-stats notification visible
 * while Archer is running. Polls /display_data every 5 seconds.
 *
 * Start via:
 *   Intent svc = new Intent(context, ArcherForegroundService.class);
 *   context.startForegroundService(svc);
 *
 * Stop via:
 *   context.stopService(new Intent(context, ArcherForegroundService.class));
 */
public class ArcherForegroundService extends Service {

    private static final String CHANNEL_ID = "archer_live";
    private static final int    NOTIF_ID   = 1001;
    private static final long   POLL_MS    = 5_000;

    private final Handler  handler = new Handler(Looper.getMainLooper());
    private final Runnable poller  = new Runnable() {
        @Override public void run() {
            fetchAndUpdate();
            handler.postDelayed(this, POLL_MS);
        }
    };

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        createChannel();
        startForeground(NOTIF_ID, buildNotification("Archer", "Connecting to truck..."));
        handler.post(poller);
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        handler.removeCallbacks(poller);
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }

    private void fetchAndUpdate() {
        new Thread(() -> {
            try {
                URL url = new URL(MainActivity.ARCHER_URL + "/display_data");
                HttpURLConnection conn = (HttpURLConnection) url.openConnection();
                conn.setConnectTimeout(4_000);
                conn.setReadTimeout(4_000);
                if (conn.getResponseCode() != 200) return;

                StringBuilder sb = new StringBuilder();
                try (BufferedReader br = new BufferedReader(
                        new InputStreamReader(conn.getInputStream()))) {
                    String line;
                    while ((line = br.readLine()) != null) sb.append(line);
                }

                JSONObject d = new JSONObject(sb.toString());
                int    speed = d.optInt("speed", 0);
                int    rpm   = d.optInt("rpm", 0);
                int    oil   = d.optInt("oil_temp", 0);
                double bat   = d.optDouble("battery_main", 0.0);

                String title = speed > 0 ? speed + " MPH" : "Parked";
                String body  = String.format("RPM %,d  ·  Oil %d°F  ·  Bat %.1fV", rpm, oil, bat);

                NotificationManager nm =
                    (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
                nm.notify(NOTIF_ID, buildNotification(title, body));

            } catch (Exception ignored) {}
        }).start();
    }

    private Notification buildNotification(String title, String body) {
        Intent open = new Intent(this, MainActivity.class);
        open.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent pi = PendingIntent.getActivity(
            this, 0, open,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        return new NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle(title)
            .setContentText(body)
            .setContentIntent(pi)
            .setOngoing(true)
            .setSilent(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build();
    }

    private void createChannel() {
        NotificationChannel ch = new NotificationChannel(
            CHANNEL_ID,
            "Archer Live Stats",
            NotificationManager.IMPORTANCE_LOW);
        ch.setDescription("Live truck status — speed, RPM, oil temp, battery");
        ((NotificationManager) getSystemService(NOTIFICATION_SERVICE))
            .createNotificationChannel(ch);
    }
}
