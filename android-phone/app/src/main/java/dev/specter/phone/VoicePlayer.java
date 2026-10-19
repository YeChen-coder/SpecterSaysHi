package dev.specter.phone;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.media.AudioAttributes;
import android.media.AudioFocusRequest;
import android.media.AudioManager;
import android.media.MediaPlayer;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;

import java.io.File;

/**
 * Plays his voice on the media stream. While he speaks, other audio (a video)
 * pauses through transient audio focus and resumes when he is done.
 */
final class VoicePlayer {
    private static final String TAG = "SpecterPhone";
    private static final String PRESENCE_CHANNEL = "specter_presence";
    static final int PRESENCE_NOTIFICATION_ID = 7;
    private static final Handler MAIN = new Handler(Looper.getMainLooper());
    private static MediaPlayer player;
    private static AudioFocusRequest focus;

    private VoicePlayer() {}

    /** Play a clip; withPresence shows a quiet "Specter is with you" notice with Stop. */
    static void play(Context context, File file, boolean withPresence) {
        play(context, file, withPresence, 0);
    }

    /** delayMs lets the notification chime finish so it does not duck his first words. */
    static void play(Context context, File file, boolean withPresence, long delayMs) {
        final Context app = context.getApplicationContext();
        MAIN.postDelayed(() -> {
            stopNow(app);
            if (!file.isFile()) {
                Log.w(TAG, "Voice clip missing: " + file.getName());
                return;
            }
            AudioAttributes attributes = new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build();
            AudioManager audio = app.getSystemService(AudioManager.class);
            focus = new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT)
                    .setAudioAttributes(attributes)
                    .setOnAudioFocusChangeListener(change -> {
                        // A phone call or another app taking over silences him.
                        if (change == AudioManager.AUDIOFOCUS_LOSS || change == AudioManager.AUDIOFOCUS_LOSS_TRANSIENT) {
                            stopNow(app);
                        }
                    }, MAIN)
                    .build();
            if (audio.requestAudioFocus(focus) != AudioManager.AUDIOFOCUS_REQUEST_GRANTED) {
                Log.w(TAG, "Audio focus refused; not playing");
                focus = null;
                return;
            }
            try {
                player = new MediaPlayer();
                player.setAudioAttributes(attributes);
                player.setDataSource(file.getPath());
                player.setOnCompletionListener(done -> stopNow(app));
                player.setOnErrorListener((failed, what, extra) -> {
                    Log.w(TAG, "Voice playback error " + what + "/" + extra);
                    stopNow(app);
                    return true;
                });
                player.prepare();
                player.start();
                if (withPresence) showPresence(app);
            } catch (Exception e) {
                Log.w(TAG, "Could not play voice clip", e);
                stopNow(app);
            }
        }, delayMs);
    }

    static void stop(Context context) {
        final Context app = context.getApplicationContext();
        MAIN.post(() -> stopNow(app));
    }

    private static void stopNow(Context app) {
        if (player != null) {
            try {
                player.stop();
            } catch (IllegalStateException ignored) {
                // Already stopped or never prepared.
            }
            player.release();
            player = null;
        }
        if (focus != null) {
            app.getSystemService(AudioManager.class).abandonAudioFocusRequest(focus);
            focus = null;
        }
        app.getSystemService(NotificationManager.class).cancel(PRESENCE_NOTIFICATION_ID);
    }

    private static void showPresence(Context app) {
        NotificationManager manager = app.getSystemService(NotificationManager.class);
        manager.createNotificationChannel(new NotificationChannel(
                PRESENCE_CHANNEL, "Specter is with you", NotificationManager.IMPORTANCE_LOW));
        Intent stop = new Intent(app, NudgeActionReceiver.class)
                .putExtra(NudgeActionReceiver.EXTRA_ACTION, NudgeActionReceiver.ACTION_STOP_AUDIO);
        PendingIntent pending = PendingIntent.getBroadcast(
                app, PRESENCE_NOTIFICATION_ID, stop,
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        Notification notice = new Notification.Builder(app, PRESENCE_CHANNEL)
                .setSmallIcon(android.R.drawable.ic_lock_silent_mode_off)
                .setContentTitle("Specter")
                .setContentText("I'm here. Close your eyes.")
                .setOngoing(true)
                .addAction(new Notification.Action.Builder(null, "Stop", pending).build())
                .build();
        manager.notify(PRESENCE_NOTIFICATION_ID, notice);
    }
}
