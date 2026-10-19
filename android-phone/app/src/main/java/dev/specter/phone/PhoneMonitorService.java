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

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;

import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

public class PhoneMonitorService extends Service {
    private static final String TAG = "SpecterPhone";
    private static final String SERVICE_CHANNEL = "specter_service";
    private static final String ROUTINE_CHANNEL = "specter_routines";
    private static final String NUDGE_CHANNEL = "specter_nudges";
    private static final String VOICE_NUDGE_CHANNEL = "specter_nudges_voice";
    /** True while the service is in the foreground; read by the watchdog. */
    static volatile boolean running;
    private ScheduledExecutorService worker;
    private String lastForegroundPackage = "";

    @Override public void onCreate() {
        super.onCreate();
        NotificationManager manager = getSystemService(NotificationManager.class);
        manager.createNotificationChannel(new NotificationChannel(SERVICE_CHANNEL, "Specter 手机数据连接", NotificationManager.IMPORTANCE_LOW));
        manager.createNotificationChannel(new NotificationChannel(ROUTINE_CHANNEL, "Specter 例程命令", NotificationManager.IMPORTANCE_DEFAULT));
        // High importance shows reminders as a heads-up banner.
        manager.createNotificationChannel(new NotificationChannel(NUDGE_CHANNEL, "Specter reminders", NotificationManager.IMPORTANCE_HIGH));
        // When he speaks, his voice is the alert: no chime to talk over, only a vibration.
        NotificationChannel voiceChannel = new NotificationChannel(
                VOICE_NUDGE_CHANNEL, "Specter reminders (spoken)", NotificationManager.IMPORTANCE_HIGH);
        voiceChannel.setSound(null, null);
        voiceChannel.enableVibration(true);
        manager.createNotificationChannel(voiceChannel);
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        Intent open = new Intent(this, MainActivity.class);
        PendingIntent pending = PendingIntent.getActivity(this, 0, open, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        Notification notice = new Notification.Builder(this, SERVICE_CHANNEL)
                .setContentTitle("Specter 手机数据连接运行中")
                .setContentText("向电脑上报手机状态")
                .setSmallIcon(android.R.drawable.ic_menu_info_details)
                .setContentIntent(pending).setOngoing(true).build();
        try {
            startForeground(1, notice);
        } catch (Exception e) {
            // A system restart from the background can be refused; the watchdog retries.
            Log.w(TAG, "Foreground start refused; stopping until the watchdog retries", e);
            stopSelf();
            return START_NOT_STICKY;
        }
        running = true;
        Keeper.scheduleWatchdog(this);
        if (worker == null) {
            worker = Executors.newSingleThreadScheduledExecutor();
            worker.scheduleWithFixedDelay(this::tick, 0, 10, TimeUnit.SECONDS);
        }
        // If the system kills the process, ask it to recreate this service.
        return START_STICKY;
    }

    @Override public void onDestroy() {
        running = false;
        if (worker != null) worker.shutdownNow();
        super.onDestroy();
    }

    @Override public IBinder onBind(Intent intent) { return null; }

    private void tick() {
        try {
            if (LinkClient.token(this).isEmpty()) return;
            LinkClient.call(this, "POST", "/v1/status", sample());
            JSONObject next = LinkClient.call(this, "GET", "/v1/commands/next", null).optJSONObject("command");
            if (next == null) return;
            String id = next.optString("id");
            String name = next.optString("name");
            if (name.equals("guard")) {
                // Lockdown changes (including the computer's release backdoor) need no notification.
                applyGuard(next.getJSONObject("payload"));
                LinkClient.call(this, "POST", "/v1/commands/ack", new JSONObject().put("id", id));
                return;
            }
            if (!name.equals("focus_on") && !name.equals("focus_off") && !name.equals("nudge")) return;
            // Without permission the command is left unacknowledged, so the computer
            // learns from its expiry that nothing was shown.
            if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return;
            String prior = getSharedPreferences("link", MODE_PRIVATE).getString("last_command_id", "");
            if (!id.equals(prior)) {
                if (name.equals("nudge")) {
                    JSONObject payload = next.getJSONObject("payload");
                    // Lock first, then speak: the warning should already be true when heard.
                    JSONObject guard = payload.optJSONObject("guard");
                    if (guard != null) applyGuard(guard);
                    showNudge(payload);
                } else {
                    showRoutineCommand(id, name);
                }
                getSharedPreferences("link", MODE_PRIVATE).edit().putString("last_command_id", id).apply();
            }
            LinkClient.call(this, "POST", "/v1/commands/ack", new JSONObject().put("id", id));
        } catch (Exception e) {
            Log.w(TAG, "Computer link unavailable; will retry", e);
        }
    }

    private void showRoutineCommand(String id, String name) {
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
    }

    /** A reminder from Specter with up to three answer buttons. */
    private void showNudge(JSONObject payload) throws Exception {
        NotificationManager manager = getSystemService(NotificationManager.class);
        // A newer reminder replaces any older one still on screen.
        for (StatusBarNotification active : manager.getActiveNotifications()) {
            String channel = active.getNotification().getChannelId();
            if (NUDGE_CHANNEL.equals(channel) || VOICE_NUDGE_CHANNEL.equals(channel)) {
                manager.cancel(active.getTag(), active.getId());
            }
        }
        String nudgeId = payload.getString("nudge_id");
        String title = payload.getString("title");
        String text = payload.getString("text");
        int notificationId = nudgeId.hashCode();
        // Fetch his voice before showing, so it can speak as the banner appears.
        // A failed download still shows the text reminder, with the normal chime.
        File voice = fetchAudio(payload.optString("audio_id", ""));
        File stay = fetchAudio(payload.optString("stay_audio_id", ""));
        String channel = voice != null ? VOICE_NUDGE_CHANNEL : NUDGE_CHANNEL;
        Notification publicVersion = new Notification.Builder(this, channel)
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentTitle(title)
                .setContentText("Specter reminder")
                .build();
        Notification.Builder builder = new Notification.Builder(this, channel)
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentTitle(title)
                .setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setCategory(Notification.CATEGORY_REMINDER)
                // The lock screen shows only "Specter reminder", not the message.
                .setVisibility(Notification.VISIBILITY_PRIVATE)
                .setPublicVersion(publicVersion)
                .setAutoCancel(true);
        JSONArray actions = payload.optJSONArray("actions");
        for (int i = 0; actions != null && i < actions.length() && i < 3; i++) {
            JSONObject action = actions.getJSONObject(i);
            // Without the wind-down recording, Stay with me would be an empty promise.
            if ("stay".equals(action.getString("id")) && stay == null) continue;
            Intent reply = new Intent(this, NudgeActionReceiver.class)
                    .putExtra(NudgeActionReceiver.EXTRA_STAY_AUDIO, stay == null ? null : stay.getPath())
                    .putExtra(NudgeActionReceiver.EXTRA_NUDGE_ID, nudgeId)
                    .putExtra(NudgeActionReceiver.EXTRA_ACTION, action.getString("id"))
                    .putExtra(NudgeActionReceiver.EXTRA_SNOOZE_MINUTES, action.optInt("snooze_minutes", 0))
                    .putExtra(NudgeActionReceiver.EXTRA_NOTIFICATION_ID, notificationId);
            PendingIntent pending = PendingIntent.getBroadcast(
                    this, notificationId * 4 + i, reply,
                    PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
            builder.addAction(new Notification.Action.Builder(null, action.getString("label"), pending).build());
        }
        manager.notify(notificationId, builder.build());
        // A short beat lets the banner appear before he starts talking.
        if (voice != null) VoicePlayer.play(this, voice, false, 400);
    }

    private void applyGuard(JSONObject guard) throws Exception {
        if ("off".equals(guard.optString("mode", "off"))) {
            Guard.release(this);
            Log.i(TAG, "Lockdown released");
            return;
        }
        java.util.List<String> clips = new java.util.ArrayList<>();
        JSONArray ids = guard.optJSONArray("bounce_audio_ids");
        for (int i = 0; ids != null && i < ids.length(); i++) {
            File clip = fetchAudio(ids.getString(i));
            if (clip != null) clips.add(clip.getPath());
        }
        Guard.apply(this, guard, clips);
        Log.i(TAG, "Lockdown " + guard.getString("mode") + " until " + guard.getLong("until_ms"));
    }

    /** Download a voice clip once; ids are content hashes, so a cached file is reused. */
    private File fetchAudio(String audioId) {
        if (!audioId.matches("[0-9a-f]{8,64}")) return null;
        File folder = new File(getCacheDir(), "specter_audio");
        if (!folder.isDirectory() && !folder.mkdirs()) return null;
        File file = new File(folder, audioId + ".mp3");
        if (file.isFile() && file.length() > 0) return file;
        try {
            LinkClient.download(this, "/v1/audio/" + audioId, file);
            pruneAudio(folder);
            return file;
        } catch (Exception e) {
            Log.w(TAG, "Could not download voice clip", e);
            return null;
        }
    }

    private static void pruneAudio(File folder) {
        File[] files = folder.listFiles();
        // Room for tonight's reminders, the wind-down and the lockdown lines.
        if (files == null || files.length <= 60) return;
        java.util.Arrays.sort(files, (a, b) -> Long.compare(b.lastModified(), a.lastModified()));
        for (int i = 60; i < files.length; i++) {
            if (!files[i].delete()) Log.w(TAG, "Could not remove old clip " + files[i].getName());
        }
    }

    private String appLabel(String pkg) {
        if (pkg == null || pkg.isEmpty()) return "";
        try {
            android.content.pm.PackageManager pm = getPackageManager();
            return pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)).toString();
        } catch (Exception e) {
            return "";
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
        value.put("foreground_label", appLabel(foreground));
        // Lets the computer notice a lockdown that was switched off on the phone.
        value.put("guard_mode", Guard.mode(this));
        value.put("guard_enabled", GuardService.enabled());
        value.put("can_dim", Settings.System.canWrite(this));
        value.put("can_gray", Guard.canGray(this));
        value.put("last_interaction_age_ms", interaction < 0 ? JSONObject.NULL : Math.max(0, now - interaction));
        value.put("brightness", Settings.System.getInt(getContentResolver(), Settings.System.SCREEN_BRIGHTNESS, -1));
        value.put("brightness_mode", Settings.System.getInt(getContentResolver(), Settings.System.SCREEN_BRIGHTNESS_MODE, -1));
        return value;
    }
}
