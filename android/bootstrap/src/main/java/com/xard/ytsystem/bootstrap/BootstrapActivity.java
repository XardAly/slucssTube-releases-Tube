package com.xard.ytsystem.bootstrap;

import android.app.Activity;
import android.content.ClipData;
import android.content.Intent;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.Settings;
import android.view.Gravity;
import android.widget.Button;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;

import androidx.core.content.FileProvider;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InterruptedIOException;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.util.Locale;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

import javax.net.ssl.HttpsURLConnection;

public final class BootstrapActivity extends Activity {
    private static final int MAX_MANIFEST_BYTES = 1024 * 1024;
    private static final int MAX_REDIRECTS = 5;
    private static final long MAX_APK_BYTES = 160L * 1024 * 1024;
    private static final long MANIFEST_TIMEOUT_NANOS = TimeUnit.SECONDS.toNanos(75);
    private static final long DOWNLOAD_TIMEOUT_NANOS = TimeUnit.MINUTES.toNanos(15);
    private static final String OUTPUT_APK_NAME = "Slucss-System.apk";
    private static final Object FILE_LOCK = new Object();

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final Object connectionLock = new Object();
    private final String partialFileName = "Slucss-System-"
            + UUID.randomUUID() + ".apk.part";

    private volatile boolean destroyed;
    private volatile boolean cancelRequested;
    private volatile HttpsURLConnection activeConnection;
    private boolean running;
    private boolean downloading;
    private boolean waitingForInstallPermission;
    private boolean installerOpened;
    private ReleaseManifest readyManifest;
    private File downloadedApk;
    private TextView status;
    private ProgressBar progress;
    private Button action;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        cleanupStalePartials();
        buildInterface();
        action.setOnClickListener(view -> {
            if (downloading) {
                cancelDownload();
            } else if (downloadedApk != null) {
                continueToInstaller();
            } else if (readyManifest == null) {
                checkLatestVersion();
            } else {
                startDownload();
            }
        });
        checkLatestVersion();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (waitingForInstallPermission) {
            if (canRequestPackageInstalls()) {
                waitingForInstallPermission = false;
                openSystemInstaller();
            } else {
                showPermissionRequired();
            }
        } else if (installerOpened && downloadedApk != null && readyManifest != null) {
            installerOpened = false;
            showDownloaded(readyManifest, downloadedApk);
        }
    }

    @Override
    protected void onDestroy() {
        destroyed = true;
        cancelRequested = true;
        synchronized (connectionLock) {
            if (activeConnection != null) activeConnection.disconnect();
            activeConnection = null;
        }
        executor.shutdownNow();
        super.onDestroy();
    }

    private void checkLatestVersion() {
        if (running || destroyed) return;
        cancelRequested = false;
        running = true;
        readyManifest = null;
        downloadedApk = null;
        action.setEnabled(false);
        action.setText(R.string.please_wait);
        progress.setIndeterminate(true);
        status.setText(R.string.checking_latest);

        executor.execute(() -> {
            try {
                String manifestJson = fetchManifest();
                ensureActive();
                ReleaseManifest manifest = ReleaseManifest.parseSigned(
                        manifestJson,
                        Build.SUPPORTED_ABIS,
                        BuildConfig.VERSION_CODE
                );
                int highestSeen = getSharedPreferences("bootstrap_security", MODE_PRIVATE)
                        .getInt("highest_version_code", 0);
                if (manifest.versionCode < highestSeen) {
                    throw new SecurityException(
                            "A API tentou retornar uma versão anterior à já verificada."
                    );
                }
                getSharedPreferences("bootstrap_security", MODE_PRIVATE)
                        .edit()
                        .putInt("highest_version_code", Math.max(highestSeen, manifest.versionCode))
                        .apply();
                File cached = outputApk();
                if (cached.isFile()) {
                    try {
                        ApkIntegrity.verifyCachedApk(this, cached, manifest);
                        runIfAlive(() -> showDownloaded(manifest, cached));
                        return;
                    } catch (Exception ignored) {
                        cached.delete();
                    }
                }
                runIfAlive(() -> showReady(manifest));
            } catch (Exception error) {
                runIfAlive(() -> showFailure(error));
            }
        });
    }

    private String fetchManifest() throws IOException {
        long deadlineNanos = System.nanoTime() + MANIFEST_TIMEOUT_NANOS;
        HttpsURLConnection connection = openConnection(
                BuildConfig.API_BASE_URL + "/app/android/version",
                deadlineNanos
        );
        try {
            long announced = connection.getContentLengthLong();
            if (announced > MAX_MANIFEST_BYTES) {
                throw new IOException("O manifesto excede o limite de segurança.");
            }
            try (InputStream input = connection.getInputStream();
                 ByteArrayOutputStream output = new ByteArrayOutputStream()) {
                byte[] buffer = new byte[8192];
                int total = 0;
                while (true) {
                    ensureActive();
                    ensureBeforeDeadline(deadlineNanos);
                    int read = input.read(buffer);
                    if (read == -1) break;
                    total += read;
                    if (total > MAX_MANIFEST_BYTES) {
                        throw new IOException("O manifesto excede o limite de segurança.");
                    }
                    output.write(buffer, 0, read);
                }
                if (total == 0) throw new IOException("A API retornou um manifesto vazio.");
                return new String(output.toByteArray(), StandardCharsets.UTF_8);
            }
        } finally {
            closeConnection(connection);
        }
    }

    private HttpsURLConnection openConnection(String rawUrl, long deadlineNanos) throws IOException {
        URL current;
        try {
            current = new URL(rawUrl);
        } catch (Exception error) {
            throw new IOException("A URL do servidor é inválida.", error);
        }
        for (int redirects = 0; redirects <= MAX_REDIRECTS; redirects++) {
            int remainingMillis = remainingTimeoutMillis(deadlineNanos);
            if (!"https".equalsIgnoreCase(current.getProtocol())) {
                throw new IOException("O servidor tentou usar uma conexão sem HTTPS.");
            }
            HttpsURLConnection connection = (HttpsURLConnection) current.openConnection();
            connection.setInstanceFollowRedirects(false);
            connection.setConnectTimeout(Math.min(15_000, remainingMillis));
            connection.setReadTimeout(Math.min(60_000, remainingMillis));
            connection.setRequestProperty(
                    "User-Agent",
                    "Slucss-Installer/" + BuildConfig.VERSION_NAME
            );
            synchronized (connectionLock) {
                ensureActive();
                activeConnection = connection;
            }
            int code;
            try {
                code = connection.getResponseCode();
            } catch (IOException error) {
                closeConnection(connection);
                throw error;
            }
            if (code >= 200 && code < 300) return connection;
            if (code >= 300 && code < 400) {
                String location = connection.getHeaderField("Location");
                closeConnection(connection);
                if (location == null || location.trim().isEmpty()) {
                    throw new IOException("O servidor retornou um redirecionamento inválido.");
                }
                current = new URL(current, location);
                continue;
            }
            closeConnection(connection);
            throw new IOException("O servidor recusou a consulta (HTTP " + code + ").");
        }
        throw new IOException("O servidor excedeu o limite de redirecionamentos.");
    }

    private void closeConnection(HttpsURLConnection connection) {
        synchronized (connectionLock) {
            if (activeConnection == connection) activeConnection = null;
        }
        connection.disconnect();
    }

    private void ensureActive() throws InterruptedIOException {
        if (destroyed || cancelRequested || Thread.currentThread().isInterrupted()) {
            throw new InterruptedIOException("Operação cancelada.");
        }
    }

    private void ensureBeforeDeadline(long deadlineNanos) throws InterruptedIOException {
        if (System.nanoTime() >= deadlineNanos) {
            throw new InterruptedIOException("A consulta excedeu o tempo limite.");
        }
    }

    private int remainingTimeoutMillis(long deadlineNanos) throws InterruptedIOException {
        ensureActive();
        ensureBeforeDeadline(deadlineNanos);
        long remainingNanos = deadlineNanos - System.nanoTime();
        return (int) Math.max(1L, Math.min(
                Integer.MAX_VALUE,
                TimeUnit.NANOSECONDS.toMillis(remainingNanos)
        ));
    }

    private void startDownload() {
        ReleaseManifest manifest = readyManifest;
        if (manifest == null || downloading || destroyed) return;
        cancelRequested = false;
        downloading = true;
        downloadedApk = null;
        progress.setIndeterminate(true);
        progress.setProgress(0);
        status.setText(R.string.starting_download);
        action.setText(R.string.cancel_download);
        action.setEnabled(true);

        executor.execute(() -> {
            File partial = partialApk();
            try {
                downloadToPartial(manifest, partial);
                ensureActive();
                runIfAlive(() -> {
                    progress.setIndeterminate(true);
                    status.setText(R.string.verifying_download);
                    action.setEnabled(false);
                });
                ApkIntegrity.verifyArchive(this, partial, manifest.versionCode);
                ensureActive();
                File output = promoteDownload(partial);
                runIfAlive(() -> showDownloaded(manifest, output));
            } catch (Exception error) {
                partial.delete();
                if (cancelRequested && !destroyed) {
                    runIfAlive(() -> showDownloadCancelled(manifest));
                } else {
                    runIfAlive(() -> showDownloadFailure(manifest, error));
                }
            }
        });
    }

    private void downloadToPartial(ReleaseManifest manifest, File partial) throws Exception {
        File directory = updateDirectory();
        if ((!directory.isDirectory() && !directory.mkdirs()) || !directory.isDirectory()) {
            throw new IOException("Não foi possível preparar o armazenamento temporário.");
        }
        partial.delete();
        long deadlineNanos = System.nanoTime() + DOWNLOAD_TIMEOUT_NANOS;
        HttpsURLConnection connection = openConnection(manifest.download.url, deadlineNanos);
        try {
            long announced = connection.getContentLengthLong();
            if (announced == 0 || announced > MAX_APK_BYTES) {
                throw new IOException("O APK possui um tamanho inválido.");
            }
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            long total = 0;
            int lastPercent = -1;
            long lastUnknownUpdate = 0;
            try (InputStream input = connection.getInputStream();
                 FileOutputStream output = new FileOutputStream(partial)) {
                byte[] buffer = new byte[256 * 1024];
                while (true) {
                    ensureActive();
                    ensureBeforeDeadline(deadlineNanos);
                    int read = input.read(buffer);
                    if (read == -1) break;
                    total += read;
                    if (total > MAX_APK_BYTES) {
                        throw new IOException("O APK excede o limite de segurança.");
                    }
                    digest.update(buffer, 0, read);
                    output.write(buffer, 0, read);
                    if (announced > 0) {
                        int percent = (int) Math.min(99, total * 100 / announced);
                        if (percent != lastPercent) {
                            lastPercent = percent;
                            long downloaded = total;
                            runIfAlive(() -> showDownloadProgress(
                                    percent,
                                    downloaded,
                                    announced
                            ));
                        }
                    } else if (total - lastUnknownUpdate >= 1024 * 1024) {
                        lastUnknownUpdate = total;
                        long downloaded = total;
                        runIfAlive(() -> showDownloadProgress(-1, downloaded, -1));
                    }
                }
                output.getFD().sync();
            }
            if (total == 0 || (announced > 0 && total != announced)) {
                throw new IOException("O download do APK ficou incompleto.");
            }
            String actualHash = ApkIntegrity.hex(digest.digest());
            if (!manifest.download.sha256.equals(actualHash)) {
                throw new SecurityException("O APK não passou na verificação de segurança.");
            }
        } finally {
            closeConnection(connection);
        }
    }

    private File promoteDownload(File partial) throws Exception {
        File output = outputApk();
        synchronized (FILE_LOCK) {
            ensureActive();
            try {
                Files.move(
                        partial.toPath(),
                        output.toPath(),
                        StandardCopyOption.ATOMIC_MOVE,
                        StandardCopyOption.REPLACE_EXISTING
                );
            } catch (AtomicMoveNotSupportedException ignored) {
                Files.move(
                        partial.toPath(),
                        output.toPath(),
                        StandardCopyOption.REPLACE_EXISTING
                );
            }
        }
        return output;
    }

    private void showDownloadProgress(int percent, long downloaded, long total) {
        if (!downloading) return;
        if (percent >= 0) {
            progress.setIndeterminate(false);
            progress.setProgress(percent);
            status.setText(getString(
                    R.string.downloading_progress,
                    percent,
                    formatBytes(downloaded),
                    formatBytes(total)
            ));
        } else {
            progress.setIndeterminate(true);
            status.setText(getString(
                    R.string.downloading_unknown_size,
                    formatBytes(downloaded)
            ));
        }
    }

    private void cancelDownload() {
        if (!downloading) return;
        cancelRequested = true;
        status.setText(R.string.cancelling_download);
        action.setEnabled(false);
        synchronized (connectionLock) {
            if (activeConnection != null) activeConnection.disconnect();
        }
    }

    private void showDownloadCancelled(ReleaseManifest manifest) {
        cancelRequested = false;
        downloading = false;
        readyManifest = manifest;
        downloadedApk = null;
        progress.setIndeterminate(false);
        progress.setProgress(0);
        status.setText(R.string.download_cancelled);
        action.setText(R.string.download_correct_version);
        action.setEnabled(true);
    }

    private void showDownloadFailure(ReleaseManifest manifest, Exception error) {
        cancelRequested = false;
        downloading = false;
        readyManifest = manifest;
        downloadedApk = null;
        progress.setIndeterminate(false);
        progress.setProgress(0);
        String message = error.getMessage();
        status.setText(error instanceof SecurityException
                && message != null
                && !message.trim().isEmpty()
                ? message
                : getString(R.string.download_failed));
        action.setText(R.string.retry);
        action.setEnabled(true);
    }

    private void showDownloaded(ReleaseManifest manifest, File apk) {
        cancelRequested = false;
        running = false;
        downloading = false;
        readyManifest = manifest;
        downloadedApk = apk;
        progress.setIndeterminate(false);
        progress.setProgress(100);
        status.setText(getString(R.string.update_ready, manifest.versionName));
        action.setText(R.string.update_now);
        action.setEnabled(true);
    }

    private String formatBytes(long bytes) {
        if (bytes < 1024 * 1024) {
            return String.format(Locale.ROOT, "%.1f KB", bytes / 1024.0);
        }
        return String.format(Locale.ROOT, "%.1f MB", bytes / (1024.0 * 1024.0));
    }

    private void showReady(ReleaseManifest manifest) {
        cancelRequested = false;
        running = false;
        downloading = false;
        readyManifest = manifest;
        downloadedApk = null;
        progress.setIndeterminate(false);
        progress.setProgress(0);
        String architecture;
        if ("arm64-v8a".equals(manifest.download.abi)) {
            architecture = "Android 64 bits";
        } else if ("armeabi-v7a".equals(manifest.download.abi)) {
            architecture = "Android 32 bits";
        } else {
            architecture = "Android universal";
        }
        status.setText(getString(
                R.string.correct_version_ready,
                manifest.versionName,
                architecture
        ));
        action.setText(R.string.download_correct_version);
        action.setEnabled(true);
    }

    private void showFailure(Exception error) {
        cancelRequested = false;
        running = false;
        downloading = false;
        readyManifest = null;
        downloadedApk = null;
        progress.setIndeterminate(false);
        progress.setProgress(0);
        String securityMessage = error.getMessage();
        status.setText(error instanceof SecurityException
                && securityMessage != null
                && !securityMessage.trim().isEmpty()
                ? securityMessage
                : getString(R.string.check_failed));
        action.setText(R.string.retry);
        action.setEnabled(true);
    }

    private void continueToInstaller() {
        File apk = downloadedApk;
        if (apk == null || !apk.isFile() || readyManifest == null) {
            downloadedApk = null;
            checkLatestVersion();
            return;
        }
        if (canRequestPackageInstalls()) {
            openSystemInstaller();
            return;
        }
        try {
            waitingForInstallPermission = true;
            status.setText(R.string.permission_explanation);
            action.setEnabled(false);
            startActivity(new Intent(
                    Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:" + getPackageName())
            ));
        } catch (RuntimeException error) {
            waitingForInstallPermission = false;
            status.setText(R.string.installer_unavailable);
            action.setText(R.string.update_now);
            action.setEnabled(true);
        }
    }

    private boolean canRequestPackageInstalls() {
        return getPackageManager().canRequestPackageInstalls();
    }

    private void showPermissionRequired() {
        waitingForInstallPermission = false;
        status.setText(R.string.permission_explanation);
        action.setText(R.string.allow_update);
        action.setEnabled(true);
    }

    private void openSystemInstaller() {
        File apk = downloadedApk;
        if (apk == null || !apk.isFile()) {
            downloadedApk = null;
            checkLatestVersion();
            return;
        }
        try {
            Uri uri = FileProvider.getUriForFile(
                    this,
                    getPackageName() + ".installer.files",
                    apk
            );
            Intent install = new Intent(Intent.ACTION_VIEW);
            install.setDataAndType(uri, "application/vnd.android.package-archive");
            install.setClipData(ClipData.newRawUri("Slucss System", uri));
            install.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
            installerOpened = true;
            status.setText(R.string.opening_installer);
            action.setEnabled(false);
            startActivity(install);
        } catch (RuntimeException error) {
            installerOpened = false;
            status.setText(R.string.installer_unavailable);
            action.setText(R.string.update_now);
            action.setEnabled(true);
        }
    }

    private void runIfAlive(Runnable actionToRun) {
        if (destroyed) return;
        runOnUiThread(() -> {
            if (!destroyed && !isFinishing()) actionToRun.run();
        });
    }

    private File updateDirectory() {
        return new File(getCacheDir(), "updates");
    }

    private File partialApk() {
        return new File(updateDirectory(), partialFileName);
    }

    private File outputApk() {
        return new File(updateDirectory(), OUTPUT_APK_NAME);
    }

    private void cleanupStalePartials() {
        File updates = updateDirectory();
        File[] entries = updates.listFiles();
        if (entries == null) return;
        for (File entry : entries) {
            if (entry.isFile() && entry.getName().endsWith(".part")) entry.delete();
        }
    }

    private void buildInterface() {
        int padding = dp(28);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setGravity(Gravity.CENTER);
        root.setPadding(padding, padding, padding, padding);
        root.setBackgroundColor(Color.rgb(11, 11, 14));

        ImageView icon = new ImageView(this);
        icon.setImageResource(R.drawable.ic_slucss);
        root.addView(icon, new LinearLayout.LayoutParams(dp(84), dp(84)));

        TextView title = new TextView(this);
        title.setText(R.string.product_name);
        title.setTextColor(Color.WHITE);
        title.setTextSize(26);
        title.setGravity(Gravity.CENTER);
        root.addView(title, matchWrap(dp(22)));

        TextView subtitle = new TextView(this);
        subtitle.setText(R.string.installer_subtitle);
        subtitle.setTextColor(Color.rgb(229, 9, 20));
        subtitle.setTextSize(15);
        subtitle.setGravity(Gravity.CENTER);
        root.addView(subtitle, matchWrap(dp(4)));

        status = new TextView(this);
        status.setTextColor(Color.rgb(224, 224, 228));
        status.setTextSize(16);
        status.setGravity(Gravity.CENTER);
        status.setMinHeight(dp(74));
        root.addView(status, matchWrap(dp(30)));

        progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progress.setMax(100);
        progress.setProgressTintList(ColorStateList.valueOf(Color.rgb(229, 9, 20)));
        LinearLayout.LayoutParams progressParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                dp(8)
        );
        progressParams.topMargin = dp(8);
        root.addView(progress, progressParams);

        action = new Button(this);
        action.setAllCaps(false);
        action.setTextColor(Color.WHITE);
        action.setTextSize(16);
        action.setBackgroundTintList(ColorStateList.valueOf(Color.rgb(229, 9, 20)));
        LinearLayout.LayoutParams actionParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                dp(56)
        );
        actionParams.topMargin = dp(28);
        root.addView(action, actionParams);

        TextView notice = new TextView(this);
        notice.setText(R.string.safe_update_notice);
        notice.setTextColor(Color.rgb(155, 155, 162));
        notice.setTextSize(13);
        notice.setGravity(Gravity.CENTER);
        root.addView(notice, matchWrap(dp(18)));

        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.addView(root, new ScrollView.LayoutParams(
                ScrollView.LayoutParams.MATCH_PARENT,
                ScrollView.LayoutParams.WRAP_CONTENT
        ));
        setContentView(scroll);
    }

    private LinearLayout.LayoutParams matchWrap(int topMargin) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
        params.topMargin = topMargin;
        return params;
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
