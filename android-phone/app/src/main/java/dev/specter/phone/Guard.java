package dev.specter.phone;

import android.content.Context;
import android.content.SharedPreferences;
import android.provider.Settings;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/**
 * Bedtime lockdown state, kept in preferences so it survives restarts.
 * mode: off | block (bounce non-allowed apps) | block_lock (also lock after each unlock and dim).
 */
final class Guard {
    private static final String PREFS = "guard";

    private Guard() {}

    static void apply(Context context, JSONObject guard, List<String> bounceClips) throws Exception {
        JSONArray allow = guard.optJSONArray("allow");
        Set<String> packages = new HashSet<>();
        for (int i = 0; allow != null && i < allow.length(); i++) packages.add(allow.getString(i));
        packages.add(context.getPackageName());
        prefs(context).edit()
                .putString("mode", guard.getString("mode"))
                .putLong("until_ms", guard.getLong("until_ms"))
                .putStringSet("allow", packages)
                .putString("clips", String.join("\n", bounceClips))
                .apply();
        GuardService.onGuardChanged();
    }

    static void release(Context context) {
        prefs(context).edit().putString("mode", "off").remove("until_ms").apply();
        restoreBrightness(context);
        GuardService.onGuardChanged();
    }

    /** The active mode; an expired lockdown (past morning) reads as off. */
    static String mode(Context context) {
        SharedPreferences p = prefs(context);
        String mode = p.getString("mode", "off");
        if (!"off".equals(mode) && System.currentTimeMillis() >= p.getLong("until_ms", 0)) return "off";
        return mode;
    }

    static boolean active(Context context) {
        return !"off".equals(mode(context));
    }

    static boolean allowed(Context context, String pkg) {
        return prefs(context).getStringSet("allow", new HashSet<>()).contains(pkg);
    }

    static List<String> clips(Context context) {
        List<String> result = new ArrayList<>();
        for (String path : prefs(context).getString("clips", "").split("\n")) {
            if (!path.isEmpty()) result.add(path);
        }
        return result;
    }

    /** Level 4: lowest brightness, remembering the user's setting to restore at release. */
    static void dim(Context context) {
        if (!Settings.System.canWrite(context)) return;
        SharedPreferences p = prefs(context);
        if (!p.contains("saved_brightness")) {
            p.edit()
                    .putInt("saved_brightness", Settings.System.getInt(context.getContentResolver(), Settings.System.SCREEN_BRIGHTNESS, 128))
                    .putInt("saved_mode", Settings.System.getInt(context.getContentResolver(), Settings.System.SCREEN_BRIGHTNESS_MODE, 1))
                    .apply();
        }
        Settings.System.putInt(context.getContentResolver(), Settings.System.SCREEN_BRIGHTNESS_MODE, Settings.System.SCREEN_BRIGHTNESS_MODE_MANUAL);
        Settings.System.putInt(context.getContentResolver(), Settings.System.SCREEN_BRIGHTNESS, 1);
    }

    private static final String GRAY_ENABLED = "accessibility_display_daltonizer_enabled";
    private static final String GRAY_MODE = "accessibility_display_daltonizer";

    /** Grayscale needs WRITE_SECURE_SETTINGS, which only the user can grant (over adb). */
    static boolean canGray(Context context) {
        return context.checkSelfPermission("android.permission.WRITE_SECURE_SETTINGS")
                == android.content.pm.PackageManager.PERMISSION_GRANTED;
    }

    /** Level 4: grayscale screen (color correction set to monochromacy), restored at release. */
    static void gray(Context context) {
        if (!canGray(context)) return;
        SharedPreferences p = prefs(context);
        android.content.ContentResolver resolver = context.getContentResolver();
        if (!p.contains("saved_gray_enabled")) {
            p.edit()
                    .putInt("saved_gray_enabled", Settings.Secure.getInt(resolver, GRAY_ENABLED, 0))
                    .putInt("saved_gray_mode", Settings.Secure.getInt(resolver, GRAY_MODE, -1))
                    .apply();
        }
        Settings.Secure.putInt(resolver, GRAY_MODE, 0);
        Settings.Secure.putInt(resolver, GRAY_ENABLED, 1);
    }

    static void restoreGray(Context context) {
        SharedPreferences p = prefs(context);
        if (!p.contains("saved_gray_enabled") || !canGray(context)) return;
        android.content.ContentResolver resolver = context.getContentResolver();
        Settings.Secure.putInt(resolver, GRAY_ENABLED, p.getInt("saved_gray_enabled", 0));
        int mode = p.getInt("saved_gray_mode", -1);
        if (mode >= 0) Settings.Secure.putInt(resolver, GRAY_MODE, mode);
        p.edit().remove("saved_gray_enabled").remove("saved_gray_mode").apply();
    }

    static void restoreBrightness(Context context) {
        restoreGray(context);
        SharedPreferences p = prefs(context);
        if (!p.contains("saved_brightness") || !Settings.System.canWrite(context)) return;
        Settings.System.putInt(context.getContentResolver(), Settings.System.SCREEN_BRIGHTNESS, p.getInt("saved_brightness", 128));
        Settings.System.putInt(context.getContentResolver(), Settings.System.SCREEN_BRIGHTNESS_MODE, p.getInt("saved_mode", 1));
        p.edit().remove("saved_brightness").remove("saved_mode").apply();
    }

    private static SharedPreferences prefs(Context context) {
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }
}
