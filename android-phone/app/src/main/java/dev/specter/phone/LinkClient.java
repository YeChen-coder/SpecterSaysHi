package dev.specter.phone;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.cert.CertificateException;
import java.security.cert.X509Certificate;

import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;

/** Authenticated JSON calls to the Specter computer (pinned TLS on the LAN, or USB loopback). */
final class LinkClient {
    private static final String USB_BASE = "http://127.0.0.1:18765";

    private LinkClient() {}

    static String token(Context context) {
        return prefs(context).getString("token", "");
    }

    static JSONObject call(Context context, String method, String path, JSONObject body) throws Exception {
        HttpURLConnection connection = open(context, method, path, body);
        StringBuilder content = new StringBuilder();
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(connection.getInputStream(), StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) content.append(line);
        } finally {
            connection.disconnect();
        }
        return new JSONObject(content.toString());
    }

    /** Save a binary response (his voice clips) to a file, replacing it only when complete. */
    static void download(Context context, String path, File destination) throws Exception {
        HttpURLConnection connection = open(context, "GET", path, null);
        File partial = new File(destination.getPath() + ".part");
        try (InputStream input = connection.getInputStream(); OutputStream output = new FileOutputStream(partial)) {
            byte[] buffer = new byte[16 * 1024];
            int read;
            while ((read = input.read(buffer)) != -1) output.write(buffer, 0, read);
        } finally {
            connection.disconnect();
        }
        if (!partial.renameTo(destination)) throw new IllegalStateException("Could not save " + destination.getName());
    }

    private static HttpURLConnection open(Context context, String method, String path, JSONObject body) throws Exception {
        String token = token(context);
        if (token.isEmpty()) throw new IllegalStateException("Phone is not paired with the computer");
        String host = prefs(context).getString("lan_host", "");
        String fingerprint = prefs(context).getString("lan_cert_sha256", "");
        boolean lan = !host.isEmpty() && fingerprint.matches("[0-9a-fA-F]{64}");
        String base = lan ? "https://" + host + ":18766" : USB_BASE;
        HttpURLConnection connection = (HttpURLConnection) new URL(base + path).openConnection();
        if (lan) {
            HttpsURLConnection secure = (HttpsURLConnection) connection;
            SSLContext tls = SSLContext.getInstance("TLS");
            final String expected = fingerprint.toLowerCase(java.util.Locale.ROOT);
            tls.init(null, new TrustManager[]{new X509TrustManager() {
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
            secure.setSSLSocketFactory(tls.getSocketFactory());
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
        if (status < 200 || status >= 300) {
            connection.disconnect();
            throw new IllegalStateException("Computer returned " + status);
        }
        return connection;
    }

    private static SharedPreferences prefs(Context context) {
        return context.getSharedPreferences("link", Context.MODE_PRIVATE);
    }
}
