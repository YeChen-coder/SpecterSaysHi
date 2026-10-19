package dev.specter.phone;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Restart the phone data service after a reboot or an app update. */
public class KeepAliveReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context context, Intent intent) {
        String action = intent.getAction();
        if (Intent.ACTION_BOOT_COMPLETED.equals(action) || Intent.ACTION_MY_PACKAGE_REPLACED.equals(action)) {
            Keeper.ensureRunning(context.getApplicationContext(), action);
        }
    }
}
