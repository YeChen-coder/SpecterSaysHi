package dev.specter.phone;

import android.app.job.JobParameters;
import android.app.job.JobService;

/** Every 15 minutes: bring the phone data service back if the system stopped it. */
public class WatchdogJob extends JobService {
    @Override public boolean onStartJob(JobParameters params) {
        Keeper.ensureRunning(this, "watchdog");
        return false;
    }

    @Override public boolean onStopJob(JobParameters params) {
        return true;
    }
}
