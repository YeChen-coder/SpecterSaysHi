package dev.specter.phone;

import android.app.ActivityManager;
import android.app.AppOpsManager;
import android.app.job.JobInfo;
import android.app.job.JobScheduler;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.os.PowerManager;
import android.os.Process;
import android.util.Log;

/**
 * Keeps the phone data service running without the user watching it: started on
 * boot, after app updates, whenever the app opens, and by a 15-minute watchdog.
 * A deliberate stop from the app is respected until the user starts it again.
 */
final class Keeper {
    private static final String TAG = "SpecterPhone";
    private static final int WATCHDOG_JOB_ID = 4242;
    static final String USER_STOPPED = "user_stopped";

    private Keeper() {}

    static boolean canRun(Context context) {
        boolean paired = !LinkClient.token(context).isEmpty();
        boolean stopped = context.getSharedPreferences("link", Context.MODE_PRIVATE).getBoolean(USER_STOPPED, false);
        AppOpsManager ops = context.getSystemService(AppOpsManager.class);
        boolean usage = ops.checkOpNoThrow(AppOpsManager.OPSTR_GET_USAGE_STATS, Process.myUid(),
                context.getPackageName()) == AppOpsManager.MODE_ALLOWED;
        return paired && usage && !stopped;
    }

    static void ensureRunning(Context context, String reason) {
        scheduleWatchdog(context);
        if (!canRun(context) || PhoneMonitorService.running) return;
        try {
            context.startForegroundService(new Intent(context, PhoneMonitorService.class));
            Log.i(TAG, "Phone data service started (" + reason + ")");
        } catch (Exception e) {
            // Android refuses background starts unless battery optimization is off.
            Log.w(TAG, "Could not start phone data service (" + reason + ")", e);
        }
    }

    static void setUserStopped(Context context, boolean stopped) {
        context.getSharedPreferences("link", Context.MODE_PRIVATE).edit().putBoolean(USER_STOPPED, stopped).apply();
    }

    static void scheduleWatchdog(Context context) {
        JobScheduler jobs = context.getSystemService(JobScheduler.class);
        if (jobs.getPendingJob(WATCHDOG_JOB_ID) != null) return;
        JobInfo job = new JobInfo.Builder(WATCHDOG_JOB_ID, new ComponentName(context, WatchdogJob.class))
                .setPeriodic(15 * 60 * 1000L)
                .setPersisted(true)
                .build();
        jobs.schedule(job);
    }

    static boolean batteryUnrestricted(Context context) {
        return context.getSystemService(PowerManager.class).isIgnoringBatteryOptimizations(context.getPackageName());
    }

    static boolean backgroundRestricted(Context context) {
        return context.getSystemService(ActivityManager.class).isBackgroundRestricted();
    }
}
