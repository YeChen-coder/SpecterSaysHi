package dev.specter.phone;

import android.Manifest;
import android.app.Activity;
import android.app.AppOpsManager;
import android.content.Intent;
import android.os.Build;
import android.os.Bundle;
import android.os.Process;
import android.provider.Settings;
import android.view.Gravity;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

public class MainActivity extends Activity {
    private TextView status;
    private EditText hostInput;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        String token = getIntent().getStringExtra("link_token");
        String existingToken = getSharedPreferences("link", MODE_PRIVATE).getString("token", "");
        if (token != null && token.length() >= 32 && (existingToken.isEmpty() || token.equals(existingToken))) {
            getSharedPreferences("link", MODE_PRIVATE).edit().putString("token", token).apply();
            String host = getIntent().getStringExtra("link_host");
            String fingerprint = getIntent().getStringExtra("link_cert_sha256");
            if (validHost(host) && fingerprint != null && fingerprint.matches("[0-9a-fA-F]{64}")) {
                getSharedPreferences("link", MODE_PRIVATE).edit()
                        .putString("lan_host", host)
                        .putString("lan_cert_sha256", fingerprint.toLowerCase(java.util.Locale.ROOT)).apply();
            }
        }
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setPadding(48, 48, 48, 48);
        layout.setGravity(Gravity.TOP);
        status = new TextView(this);
        status.setTextSize(18);
        layout.addView(status);
        hostInput = new EditText(this);
        hostInput.setSingleLine(true);
        hostInput.setHint("电脑的局域网 IPv4 地址");
        hostInput.setText(getSharedPreferences("link", MODE_PRIVATE).getString("lan_host", ""));
        layout.addView(hostInput);
        addButton(layout, "保存电脑地址", () -> {
            String host = hostInput.getText().toString().trim();
            if (!validHost(host)) {
                status.setText("请输入电脑的局域网 IPv4 地址，例如 <COMPUTER_LAN_IP>");
                return;
            }
            getSharedPreferences("link", MODE_PRIVATE).edit().putString("lan_host", host).apply();
            refresh();
        });
        addButton(layout, "授权使用情况访问", () -> startActivity(new Intent(Settings.ACTION_USAGE_ACCESS_SETTINGS)));
        addButton(layout, "授权通知", () -> {
            if (Build.VERSION.SDK_INT >= 33) requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
        });
        addButton(layout, "启动手机数据服务", () -> {
            if (!usageGranted()) {
                startActivity(new Intent(Settings.ACTION_USAGE_ACCESS_SETTINGS));
                return;
            }
            if (getSharedPreferences("link", MODE_PRIVATE).getString("token", "").isEmpty()) {
                status.setText("电脑尚未配对。请先运行配对脚本。");
                return;
            }
            startForegroundService(new Intent(this, PhoneMonitorService.class));
            refresh();
        });
        addButton(layout, "停止手机数据服务", () -> {
            stopService(new Intent(this, PhoneMonitorService.class));
            refresh();
        });
        setContentView(layout);
        refresh();
        if (getIntent().getBooleanExtra("start_service", false) && usageGranted()
                && token != null && token.equals(getSharedPreferences("link", MODE_PRIVATE).getString("token", ""))) {
            startForegroundService(new Intent(this, PhoneMonitorService.class));
        }
    }

    @Override protected void onResume() { super.onResume(); refresh(); }

    private void addButton(LinearLayout layout, String label, Runnable action) {
        Button button = new Button(this);
        button.setText(label);
        button.setOnClickListener(v -> action.run());
        layout.addView(button);
    }

    private boolean usageGranted() {
        AppOpsManager ops = (AppOpsManager) getSystemService(APP_OPS_SERVICE);
        return ops.checkOpNoThrow(AppOpsManager.OPSTR_GET_USAGE_STATS, Process.myUid(), getPackageName()) == AppOpsManager.MODE_ALLOWED;
    }

    private boolean validHost(String value) {
        if (value == null || !value.matches("[0-9.]{7,15}")) return false;
        String[] parts = value.split("\\.");
        if (parts.length != 4) return false;
        for (String part : parts) {
            try {
                if (part.isEmpty() || Integer.parseInt(part) > 255) return false;
            } catch (NumberFormatException e) { return false; }
        }
        return true;
    }

    private void refresh() {
        if (status == null) return;
        boolean paired = !getSharedPreferences("link", MODE_PRIVATE).getString("token", "").isEmpty();
        String host = getSharedPreferences("link", MODE_PRIVATE).getString("lan_host", "");
        boolean secureLan = !host.isEmpty() && !getSharedPreferences("link", MODE_PRIVATE)
                .getString("lan_cert_sha256", "").isEmpty();
        status.setText("Specter Phone\n使用情况访问：" + (usageGranted() ? "已授权" : "待授权")
                + "\n电脑配对：" + (paired ? "已配置" : "待配置")
                + "\n局域网：" + (secureLan ? host + "（已配置加密连接）" : "待配置")
                + "\n通知权限：" + (Build.VERSION.SDK_INT < 33 || checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == 0 ? "已授权" : "待授权")
                + "\n\n启动服务后，每 10 秒通过局域网向电脑上报；断线会自动重试。" 
                + "电脑端仅能发出 focus_on / focus_off 两种例程通知。停止服务即可停止上报。");
    }
}
