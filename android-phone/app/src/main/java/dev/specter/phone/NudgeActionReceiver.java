package dev.specter.phone;

import android.app.NotificationManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.util.Log;

import org.json.JSONObject;

import java.io.File;

/** Handles reminder buttons: answers go back to the computer; Stay with me plays his wind-down. */
public class NudgeActionReceiver extends BroadcastReceiver {
    static final String EXTRA_NUDGE_ID = "nudge_id";
    static final String EXTRA_ACTION = "action";
    static final String EXTRA_SNOOZE_MINUTES = "snooze_minutes";
    static final String EXTRA_NOTIFICATION_ID = "notification_id";
    static final String EXTRA_STAY_AUDIO = "stay_audio";
    static final String ACTION_STOP_AUDIO = "stop_audio";
    private static final String TAG = "SpecterPhone";

    @Override public void onReceive(Context context, Intent intent) {
        final Context app = context.getApplicationContext();
        final String action = intent.getStringExtra(EXTRA_ACTION);
        if (ACTION_STOP_AUDIO.equals(action)) {
            VoicePlayer.stop(app);
            return;
        }
        final String nudgeId = intent.getStringExtra(EXTRA_NUDGE_ID);
        final int snoozeMinutes = intent.getIntExtra(EXTRA_SNOOZE_MINUTES, 0);
        final int notificationId = intent.getIntExtra(EXTRA_NOTIFICATION_ID, 0);
        app.getSystemService(NotificationManager.class).cancel(notificationId);
        if ("stay".equals(action)) {
            String stayAudio = intent.getStringExtra(EXTRA_STAY_AUDIO);
            if (stayAudio != null) VoicePlayer.play(app, new File(stayAudio), true);
        } else {
            // Any other answer ends whatever he was saying.
            VoicePlayer.stop(app);
        }
        if (nudgeId == null || action == null) return;
        final PendingResult pending = goAsync();
        // Network calls must leave the main thread; retry once if the link blips.
        new Thread(() -> {
            try {
                JSONObject body = new JSONObject().put("nudge_id", nudgeId).put("action", action);
                if ("snooze".equals(action)) body.put("snooze_minutes", snoozeMinutes);
                for (int attempt = 1; attempt <= 2; attempt++) {
                    try {
                        LinkClient.call(app, "POST", "/v1/nudges/response", body);
                        break;
                    } catch (Exception e) {
                        if (attempt == 2) throw e;
                        Thread.sleep(1500);
                    }
                }
            } catch (Exception e) {
                Log.w(TAG, "Could not send reminder answer", e);
            } finally {
                pending.finish();
            }
        }, "specter-nudge-reply").start();
    }
}
