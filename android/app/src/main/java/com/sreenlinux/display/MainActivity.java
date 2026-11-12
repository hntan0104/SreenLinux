package com.sreenlinux.display;

import android.app.Activity;
import android.content.SharedPreferences;
import android.graphics.drawable.GradientDrawable;
import android.os.Build;
import android.os.Bundle;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.view.View;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.view.inputmethod.InputMethodManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.TextView;

/**
 * Main activity for SreenLinux — turns an Android device into a Linux display.
 *
 * UI states:
 * - Idle / connecting: logo, host/port fields, Connect (or Cancel) button
 * - Connected: full-screen frames; 3-finger tap or Back toggles the overlay
 *
 * The app auto-connects on launch to the last used host/port, and pauses the
 * stream while in the background (resuming when brought back).
 */
public class MainActivity extends Activity
        implements ScreenClient.ConnectionListener, TouchHandler.Listener {

    private static final String PREFS = "sreenlinux";
    private static final String PREF_HOST = "host";
    private static final String PREF_PORT = "port";
    private static final String PREF_AUTO_CONNECT = "auto_connect";
    private static final String DEFAULT_HOST = "localhost";
    private static final int DEFAULT_PORT = 6100;

    private static final int COLOR_CONNECTED = 0xFF4CAF50;
    private static final int COLOR_CONNECTING = 0xFFFFC107;
    private static final int COLOR_DISCONNECTED = 0xFFF44336;

    private SurfaceView surfaceView;
    private View disconnectedOverlay;
    private View connectedOverlay;
    private View statusDot;
    private TextView statusText;
    private EditText hostInput;
    private EditText portInput;
    private Button connectButton;
    private View overlayStatusDot;
    private TextView fpsText;
    private TextView infoText;

    private SharedPreferences prefs;
    private FrameRenderer renderer;
    private TouchHandler touchHandler;
    private ScreenClient client;

    private String host = DEFAULT_HOST;
    private int port = DEFAULT_PORT;
    private boolean overlayVisible;
    /** Was a session active when the activity went to the background? */
    private boolean resumeOnStart;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        setContentView(R.layout.activity_main);

        surfaceView = findViewById(R.id.surfaceView);
        disconnectedOverlay = findViewById(R.id.disconnectedOverlay);
        connectedOverlay = findViewById(R.id.connectedOverlay);
        statusDot = findViewById(R.id.statusDot);
        statusText = findViewById(R.id.statusText);
        hostInput = findViewById(R.id.hostInput);
        portInput = findViewById(R.id.portInput);
        connectButton = findViewById(R.id.connectButton);
        overlayStatusDot = findViewById(R.id.overlayStatusDot);
        fpsText = findViewById(R.id.fpsText);
        infoText = findViewById(R.id.infoText);

        setDot(statusDot, COLOR_DISCONNECTED);
        setDot(overlayStatusDot, COLOR_CONNECTED);

        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        host = prefs.getString(PREF_HOST, DEFAULT_HOST);
        port = prefs.getInt(PREF_PORT, DEFAULT_PORT);
        hostInput.setText(host);
        portInput.setText(String.valueOf(port));

        renderer = new FrameRenderer(surfaceView.getHolder());
        client = new ScreenClient(renderer, this);
        touchHandler = new TouchHandler(this, surfaceView, renderer, client::send, this);

        surfaceView.getHolder().addCallback(new SurfaceHolder.Callback() {
            @Override
            public void surfaceCreated(SurfaceHolder holder) {
            }

            @Override
            public void surfaceChanged(SurfaceHolder holder, int format, int width, int height) {
                renderer.setDisplaySize(width, height);
                renderer.redraw();
            }

            @Override
            public void surfaceDestroyed(SurfaceHolder holder) {
            }
        });

        surfaceView.setOnTouchListener((v, event) ->
                client.isConnected() && touchHandler.onTouchEvent(event));

        connectButton.setOnClickListener(v -> {
            if (client.isActive()) {
                stopSession();
            } else {
                startSessionFromInputs();
            }
        });
        findViewById(R.id.disconnectButton).setOnClickListener(v -> stopSession());
        findViewById(R.id.hideOverlayButton).setOnClickListener(v -> setOverlayVisible(false));

        hideSystemUI();
        showIdle(getString(R.string.status_disconnected));

        if (prefs.getBoolean(PREF_AUTO_CONNECT, true)) {
            startSession();
        }
    }

    // ---- Session control ----

    private void startSessionFromInputs() {
        String h = hostInput.getText().toString().trim();
        if (h.isEmpty()) {
            h = DEFAULT_HOST;
        }
        int p;
        try {
            p = Integer.parseInt(portInput.getText().toString().trim());
        } catch (NumberFormatException e) {
            p = -1;
        }
        if (p < 1 || p > 65535) {
            showIdle(getString(R.string.status_invalid_port));
            return;
        }
        host = h;
        port = p;
        prefs.edit().putString(PREF_HOST, host).putInt(PREF_PORT, port).apply();
        hideKeyboard();
        startSession();
    }

    private void startSession() {
        prefs.edit().putBoolean(PREF_AUTO_CONNECT, true).apply();
        client.connect(host, port);
        onConnecting(1);
    }

    /** User-initiated stop: also disables auto-connect on next launch. */
    private void stopSession() {
        prefs.edit().putBoolean(PREF_AUTO_CONNECT, false).apply();
        client.disconnect();
        touchHandler.reset();
        showIdle(getString(R.string.status_disconnected));
    }

    // ---- ConnectionListener (UI thread) ----

    @Override
    public void onConnecting(int attempt) {
        String msg = attempt <= 1
                ? getString(R.string.status_connecting, host, port)
                : getString(R.string.status_retrying, host, port, attempt);
        showWaiting(msg);
    }

    @Override
    public void onConnected() {
        disconnectedOverlay.setVisibility(View.GONE);
        setOverlayVisible(false);
        setDot(overlayStatusDot, COLOR_CONNECTED);
        hideSystemUI();
    }

    @Override
    public void onConnectionLost(String reason, int retryInMs) {
        touchHandler.reset();
        showWaiting(getString(R.string.status_lost, reason, retryInMs / 1000f));
    }

    @Override
    public void onFpsUpdate(float fps) {
        fpsText.setText(getString(R.string.fps_format, fps));
        FrameRenderer.Mapping m = renderer.getMapping();
        if (m.isValid()) {
            infoText.setText(getString(R.string.info_connected,
                    m.frameWidth, m.frameHeight, host, port));
        }
    }

    // ---- TouchHandler.Listener ----

    @Override
    public void onThreeFingerTap() {
        setOverlayVisible(!overlayVisible);
    }

    // ---- UI helpers ----

    private void showIdle(String status) {
        disconnectedOverlay.setVisibility(View.VISIBLE);
        setOverlayVisible(false);
        statusText.setText(status);
        setDot(statusDot, COLOR_DISCONNECTED);
        connectButton.setText(R.string.action_connect);
        hostInput.setEnabled(true);
        portInput.setEnabled(true);
    }

    private void showWaiting(String status) {
        disconnectedOverlay.setVisibility(View.VISIBLE);
        setOverlayVisible(false);
        statusText.setText(status);
        setDot(statusDot, COLOR_CONNECTING);
        connectButton.setText(R.string.action_cancel);
        hostInput.setEnabled(false);
        portInput.setEnabled(false);
    }

    private void setOverlayVisible(boolean visible) {
        overlayVisible = visible;
        connectedOverlay.setVisibility(visible ? View.VISIBLE : View.GONE);
    }

    private void setDot(View view, int color) {
        GradientDrawable circle = new GradientDrawable();
        circle.setShape(GradientDrawable.OVAL);
        circle.setColor(color);
        view.setBackground(circle);
    }

    private void hideKeyboard() {
        InputMethodManager imm = (InputMethodManager) getSystemService(INPUT_METHOD_SERVICE);
        View focus = getCurrentFocus();
        if (imm != null && focus != null) {
            imm.hideSoftInputFromWindow(focus.getWindowToken(), 0);
            focus.clearFocus();
        }
    }

    @SuppressWarnings("deprecation")
    private void hideSystemUI() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            WindowInsetsController controller = getWindow().getInsetsController();
            if (controller != null) {
                controller.hide(WindowInsets.Type.statusBars() | WindowInsets.Type.navigationBars());
                controller.setSystemBarsBehavior(
                        WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
            }
        } else {
            getWindow().getDecorView().setSystemUiVisibility(
                    View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                    | View.SYSTEM_UI_FLAG_FULLSCREEN
                    | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                    | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                    | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                    | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION);
        }
    }

    // ---- Lifecycle ----

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) {
            hideSystemUI();
        }
    }

    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        if (client.isConnected()) {
            setOverlayVisible(!overlayVisible);
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onStart() {
        super.onStart();
        if (resumeOnStart) {
            resumeOnStart = false;
            startSession();
        }
    }

    @Override
    protected void onStop() {
        super.onStop();
        // Stop streaming in the background to save battery and host CPU;
        // reconnect automatically when the user comes back.
        if (client.isActive() && !isChangingConfigurations()) {
            resumeOnStart = true;
            client.disconnect();
            touchHandler.reset();
            showIdle(getString(R.string.status_disconnected));
        }
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        client.disconnect();
        renderer.release();
    }
}
