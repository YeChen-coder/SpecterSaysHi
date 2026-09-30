package dev.specter.phone;

import android.Manifest;
import android.app.AppOpsManager;
import android.app.KeyguardManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.app.usage.UsageEvents;
import android.app.usage.UsageStatsManager;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.IBinder;
import android.os.PowerManager;
import android.os.Process;
import android.provider.Settings;
import android.service.notification.StatusBarNotification;
import android.util.Log;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.cert.CertificateException;
import java.security.cert.X509Certificate;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;

public class PhoneMonitorService extends Service {
    private static final String TAG = "SpecterPhone";
    private static final String USB_BASE = "http://127.0.0.1:18765";
    private static final String SERVICE_CHANNEL = "specter_service";
    private static final String ROUTINE_CHANNEL = "specter_routines";
    private ScheduledExecutorService worker;
    private String lastForegroundPackage = "";

    @Override public void onCreate() {
        super.onCreate();
        NotificationManager manager = getSystemService(NotificationManager.class);
        manager.createNotificationChannel(new NotificationChannel(SERVICE_CHANNEL, "Specter 手机数据连接", NotificationManager.IMPORTANCE_LOW));
        manager.createNotificationChannel(new NotificationChannel(ROUTINE_CHANNEL, "Specter 例程命令", NotificationManager.IMPORTANCE_DEFAULT));
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        Intent open = new Intent(this, MainActivity.class);
        PendingIntent pending = PendingIntent.getActivity(this, 0, open, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        Notification notice = new Notification.Builder(this, SERVICE_CHANNEL)
                .setContentTitle("Specter 手机数据连接运行中")
                .setContentText("向电脑上报手机状态")
                .setSmallIcon(android.R.drawable.ic_menu_info_details)
                .setContentIntent(pending).setOngoing(true).build();
        startForeground(1, notice);
        if (worker == null) {
            worker = Executors.newSingleThreadScheduledExecutor();
            worker.scheduleWithFixedDelay(this::tick, 0, 10, TimeUnit.SECONDS);
        }
        return START_NOT_STICKY;
    }

    @Override public void onDestroy() {
        if (worker != null) worker.shutdownNow();
        super.onDestroy();
    }

    @Override public IBinder onBind(Intent intent) { return null; }

    private void tick() {
        try {
            String token = getSharedPreferences("link", MODE_PRIVATE).getString("token", "");
            if (token.isEmpty()) return;
            call("POST", "/v1/status", sample(), token);
            JSONObject next = call("GET", "/v1/commands/next", null, token).optJSONObject("command");
            if (next == null) return;
            String id = next.optString("id");
            String name = next.optString("name");
            if (!name.equals("focus_on") && !name.equals("focus_off")) return;
            if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return;
            String prior = getSharedPreferences("link", MODE_PRIVATE).getString("last_command_id", "");
            if (!id.equals(prior)) {
                NotificationManager manager = getSystemService(NotificationManager.class);
                // Keep only the latest command notification visible. Samsung
                // Modes may still need to be turned off separately.
                for (StatusBarNotification active : manager.getActiveNotifications()) {
                    if (ROUTINE_CHANNEL.equals(active.getNotification().getChannelId())) {
                        manager.cancel(active.getTag(), active.getId());
                    }
                }
                String keyword = name.equals("focus_on") ? "SPECTER_FOCUS_ON" : "SPECTER_FOCUS_OFF";
                Notification notification = new Notification.Builder(this, ROUTINE_CHANNEL)
                        .setSmallIcon(android.R.drawable.ic_dialog_info)
                        .setContentTitle(keyword)
                        .setContentText("Specter 命令：" + keyword)
                        .setAutoCancel(true).build();
                manager.notify(id.hashCode(), notification);
                getSharedPreferences("link", MODE_PRIVATE).edit().putString("last_command_id", id).apply();
            }
            call("POST", "/v1/commands/ack", new JSONObject().put("id", id), token);
        } catch (Exception e) {
            Log.w(TAG, "Computer link unavailable; will retry", e);
        }
    }

    private JSONObject sample() throws Exception {
        long now = System.currentTimeMillis();
        boolean interactive = getSystemService(PowerManager.class).isInteractive();
        boolean locked = getSystemService(KeyguardManager.class).isDeviceLocked();
        AppOpsManager ops = getSystemService(AppOpsManager.class);
        boolean usage = ops.checkOpNoThrow(AppOpsManager.OPSTR_GET_USAGE_STATS, Process.myUid(), getPackageName()) == AppOpsManager.MODE_ALLOWED;
        String foreground = usage ? lastForegroundPackage : "";
        long interaction = -1;
        if (usage) {
            UsageStatsManager stats = getSystemService(UsageStatsManager.class);
            UsageEvents events = stats.queryEvents(now - 120_000, now);
            UsageEvents.Event event = new UsageEvents.Event();
            while (events.hasNextEvent()) {
                events.getNextEvent(event);
                int type = event.getEventType();
                if (type == UsageEvents.Event.ACTIVITY_RESUMED) {
                    foreground = event.getPackageName();
                    lastForegroundPackage = foreground;
                    interaction = event.getTimeStamp();
                } else if (type == UsageEvents.Event.USER_INTERACTION) {
                    interaction = event.getTimeStamp();
                }
            }
        }
        JSONObject value = new JSONObject();
        value.put("observed_at_ms", now);
        value.put("screen_interactive", interactive);
        value.put("device_locked", locked);
        value.put("usage_access", usage);
        value.put("foreground_package", foreground);
        value.put("last_interaction_age_ms", interaction < 0 ? JSONObject.NULL : Math.max(0, now - interaction));
        value.put("brightness", Settings.System.getInt(getContentResolver(), Settings.System.SCREEN_BRIGHTNESS, -1));
        value.put("brightness_mode", Settings.System.getInt(getContentResolver(), Settings.System.SCREEN_BRIGHTNESS_MODE, -1));
        return value;
    }

    private JSONObject call(String method, String path, JSONObject body, String token) throws Exception {
        String host = getSharedPreferences("link", MODE_PRIVATE).getString("lan_host", "");
        String fingerprint = getSharedPreferences("link", MODE_PRIVATE).getString("lan_cert_sha256", "");
        boolean lan = !host.isEmpty() && fingerprint.matches("[0-9a-fA-F]{64}");
        String base = lan ? "https://" + host + ":18766" : USB_BASE;
        HttpURLConnection connection = (HttpURLConnection) new URL(base + path).openConnection();
        if (lan) {
            HttpsURLConnection secure = (HttpsURLConnection) connection;
            SSLContext context = SSLContext.getInstance("TLS");
            final String expected = fingerprint.toLowerCase(java.util.Locale.ROOT);
            context.init(null, new TrustManager[]{new X509TrustManager() {
                @Override public void checkClientTrusted(X509Certificate[] chain, String authType) throws CertificateException {
                    throw new CertificateException("Client certificates are not accepted");
                }
                @Override public void checkServerTrusted(X509Certificate[] chain, String authType) throws CertificateException {
                    if (chain == null || chain.length == 0) throw new CertificateException("Missing server certificate");
                    try {
                        chain[0].checkValidity();
                        byte[] digest = MessageDigest.getInstance("SHA-256").digest(chain[0].getEncoded());
                        StringBuilder actual = new StringBuilder(64);
                        for (byte part : digest) actual.append(String.format(java.util.Locale.ROOT, "%02x", part & 0xff));
                        if (!expected.contentEquals(actual)) throw new CertificateException("Specter computer certificate changed");
                    } catch (CertificateException e) {
                        throw e;
                    } catch (Exception e) {
                        throw new CertificateException("Could not verify Specter computer", e);
                    }
                }
                @Override public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
            }}, null);
            secure.setSSLSocketFactory(context.getSocketFactory());
            // The exact pinned certificate identifies the server even when its LAN IP changes.
            secure.setHostnameVerifier((name, session) -> true);
        }
        connection.setRequestMethod(method);
        connection.setConnectTimeout(3000);
        connection.setReadTimeout(3000);
        connection.setRequestProperty("Authorization", "Bearer " + token);
        if (body != null) {
            connection.setDoOutput(true);
            connection.setRequestProperty("Content-Type", "application/json");
            try (OutputStream stream = connection.getOutputStream()) {
                stream.write(body.toString().getBytes(StandardCharsets.UTF_8));
            }
        }
        int status = connection.getResponseCode();
        if (status < 200 || status >= 300) throw new IllegalStateException("Computer returned " + status);
        StringBuilder content = new StringBuilder();
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(connection.getInputStream(), StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) content.append(line);
        } finally {
            connection.disconnect();
        }
        return new JSONObject(content.toString());
    }
}
