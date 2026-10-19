package dev.specter.phone;

import android.accessibilityservice.AccessibilityService;
import android.app.KeyguardManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.util.Log;
import android.view.accessibility.AccessibilityEvent;

import org.json.JSONObject;

import java.io.File;
import java.util.List;
import java.util.Random;

/**
 * Enforces the bedtime lockdown. Only window-change events are read, and only
 * the package name of the app coming to the front.
 */
public class GuardService extends AccessibilityService {
    private static final String TAG = "SpecterPhone";
    private static final long LOCK_AFTER_UNLOCK_MS = 60_000;
    private static final Handler MAIN = new Handler(Looper.getMainLooper());
    private static volatile GuardService instance;
    private final Random random = new Random();
    private String lastBounced = "";
    private long lastBouncedAt;
    private boolean wasActive;
    private int lastClip = -1;

    private final Runnable lockScreen = () -> {
        if ("block_lock".equals(Guard.mode(this))) performGlobalAction(GLOBAL_ACTION_LOCK_SCREEN);
    };

    // Once a minute: notice the morning expiry (restore brightness) and keep level 4 applied.
    private final Runnable heartbeat = new Runnable() {
        @Override public void run() {
            refresh();
            MAIN.postDelayed(this, 60_000);
        }
    };

    private final BroadcastReceiver screen = new BroadcastReceiver() {
        @Override public void onReceive(Context context, Intent intent) {
            if (Intent.ACTION_USER_PRESENT.equals(intent.getAction())) {
                scheduleLock();
            } else if (Intent.ACTION_SCREEN_OFF.equals(intent.getAction())) {
                MAIN.removeCallbacks(lockScreen);
            }
        }
    };

    static boolean enabled() {
        return instance != null;
    }

    /** Called when the computer changes the lockdown. */
    static void onGuardChanged() {
        GuardService service = instance;
        if (service != null) MAIN.post(service::refresh);
    }

    @Override protected void onServiceConnected() {
        instance = this;
        IntentFilter filter = new IntentFilter(Intent.ACTION_USER_PRESENT);
        filter.addAction(Intent.ACTION_SCREEN_OFF);
        registerReceiver(screen, filter);
        MAIN.post(heartbeat);
    }

    @Override public boolean onUnbind(Intent intent) {
        shutdown();
        return super.onUnbind(intent);
    }

    @Override public void onDestroy() {
        shutdown();
        super.onDestroy();
    }

    private void shutdown() {
        if (instance != this) return;
        instance = null;
        MAIN.removeCallbacks(heartbeat);
        MAIN.removeCallbacks(lockScreen);
        try {
            unregisterReceiver(screen);
        } catch (IllegalArgumentException ignored) {
            // Not registered.
        }
    }

    @Override public void onInterrupt() {}

    @Override public void onAccessibilityEvent(AccessibilityEvent event) {
        if (event.getEventType() != AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED || event.getPackageName() == null) return;
        String pkg = event.getPackageName().toString();
        if (!Guard.active(this) || Guard.allowed(this, pkg) || pkg.equals(getPackageName())) return;
        // Only real apps are bounced: keyboards, the home screen and system overlays have no launcher entry.
        PackageManager pm = getPackageManager();
        if (pm.getLaunchIntentForPackage(pkg) == null || isHome(pkg)) {
            Log.i(TAG, "Lockdown ignores " + pkg + " (not a launchable app)");
            return;
        }
        // Only a real app screen counts. Floating panels and overlays (e.g. a wallet's
        // home-screen quick access) also send window events but are not activities.
        if (!isActivity(pkg, event.getClassName())) {
            Log.i(TAG, "Lockdown ignores " + pkg + "/" + event.getClassName() + " (not an app screen)");
            return;
        }
        long now = System.currentTimeMillis();
        if (pkg.equals(lastBounced) && now - lastBouncedAt < 1500) return;
        lastBounced = pkg;
        lastBouncedAt = now;
        boolean home = performGlobalAction(GLOBAL_ACTION_HOME);
        Log.i(TAG, "Lockdown bounced " + pkg + " (home action " + home + ")");
        List<String> clips = Guard.clips(this);
        if (!clips.isEmpty()) {
            // Never the same line twice in a row.
            int pick = random.nextInt(clips.size());
            if (clips.size() > 1 && pick == lastClip) pick = (pick + 1 + random.nextInt(clips.size() - 1)) % clips.size();
            lastClip = pick;
            VoicePlayer.play(this, new File(clips.get(pick)), false, 250);
        }
        report(pkg, label(pkg));
    }

    private void refresh() {
        boolean active = Guard.active(this);
        if (!active && wasActive) Guard.restoreBrightness(this);
        wasActive = active;
        if ("block_lock".equals(Guard.mode(this))) {
            Guard.dim(this);
            Guard.gray(this);
            KeyguardManager keyguard = getSystemService(KeyguardManager.class);
            if (getSystemService(PowerManager.class).isInteractive() && !keyguard.isKeyguardLocked()
                    && !MAIN.hasCallbacks(lockScreen)) {
                scheduleLock();
            }
        } else {
            MAIN.removeCallbacks(lockScreen);
        }
    }

    private void scheduleLock() {
        MAIN.removeCallbacks(lockScreen);
        if ("block_lock".equals(Guard.mode(this))) MAIN.postDelayed(lockScreen, LOCK_AFTER_UNLOCK_MS);
    }

    private boolean isActivity(String pkg, CharSequence className) {
        if (className == null) return false;
        try {
            getPackageManager().getActivityInfo(new android.content.ComponentName(pkg, className.toString()), 0);
            return true;
        } catch (PackageManager.NameNotFoundException e) {
            return false;
        }
    }

    private boolean isHome(String pkg) {
        Intent home = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_HOME);
        android.content.pm.ResolveInfo info = getPackageManager().resolveActivity(home, 0);
        return info != null && info.activityInfo != null && pkg.equals(info.activityInfo.packageName);
    }

    private String label(String pkg) {
        try {
            PackageManager pm = getPackageManager();
            return pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)).toString();
        } catch (Exception e) {
            return pkg;
        }
    }

    private void report(String pkg, String label) {
        final Context app = getApplicationContext();
        new Thread(() -> {
            try {
                LinkClient.call(app, "POST", "/v1/guard/event",
                        new JSONObject().put("event", "bounce").put("package", pkg).put("label", label));
            } catch (Exception e) {
                Log.w(TAG, "Could not report bounce", e);
            }
        }, "specter-guard-report").start();
    }
}
