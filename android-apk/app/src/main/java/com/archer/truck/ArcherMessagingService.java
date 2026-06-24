package com.archer.truck;

import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Intent;
import androidx.core.app.NotificationCompat;
import com.google.firebase.messaging.FirebaseMessagingService;
import com.google.firebase.messaging.RemoteMessage;
import org.json.JSONObject;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;

public class ArcherMessagingService extends FirebaseMessagingService {

    private static final String CHANNEL_ID   = "archer_alerts";
    private static final String CHANNEL_NAME = "Archer Alerts";

    // Called when FCM assigns a new token to this device.
    // Send it to the Archer server so the Pi can target this phone.
    @Override
    public void onNewToken(String token) {
        sendTokenToServer(token);
    }

    // Called when a push notification arrives (app foreground or background).
    @Override
    public void onMessageReceived(RemoteMessage message) {
        String title = "Archer";
        String body  = "";

        if (message.getNotification() != null) {
            if (message.getNotification().getTitle() != null)
                title = message.getNotification().getTitle();
            if (message.getNotification().getBody() != null)
                body = message.getNotification().getBody();
        }

        // Data payload overrides notification fields if present
        if (message.getData().containsKey("title")) title = message.getData().get("title");
        if (message.getData().containsKey("body"))  body  = message.getData().get("body");

        showNotification(title, body);
    }

    private void showNotification(String title, String body) {
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);

        NotificationChannel channel = new NotificationChannel(
            CHANNEL_ID, CHANNEL_NAME, NotificationManager.IMPORTANCE_HIGH);
        channel.setDescription("Truck alerts and status updates from Archer");
        nm.createNotificationChannel(channel);

        Intent intent = new Intent(this, MainActivity.class);
        intent.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent pendingIntent = PendingIntent.getActivity(
            this, 0, intent,
            PendingIntent.FLAG_ONE_SHOT | PendingIntent.FLAG_IMMUTABLE);

        NotificationCompat.Builder builder = new NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(new NotificationCompat.BigTextStyle().bigText(body))
            .setAutoCancel(true)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setContentIntent(pendingIntent);

        nm.notify((int) System.currentTimeMillis(), builder.build());
    }

    // POST the FCM token to the Archer server so the Pi knows how to reach this phone.
    // The server stores it and uses it when sending alerts via Firebase Admin SDK.
    private void sendTokenToServer(String token) {
        new Thread(() -> {
            try {
                URL url = new URL(MainActivity.ARCHER_URL + "/fcm_token");
                HttpURLConnection conn = (HttpURLConnection) url.openConnection();
                conn.setRequestMethod("POST");
                conn.setRequestProperty("Content-Type", "application/json");
                conn.setDoOutput(true);
                conn.setConnectTimeout(5000);
                conn.setReadTimeout(5000);

                JSONObject body = new JSONObject();
                body.put("token", token);

                try (OutputStream os = conn.getOutputStream()) {
                    os.write(body.toString().getBytes("UTF-8"));
                }
                conn.getResponseCode();
                conn.disconnect();
            } catch (Exception e) {
                // Server not reachable — token will be resent next time onNewToken fires
            }
        }).start();
    }
}
