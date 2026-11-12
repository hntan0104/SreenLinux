package com.sreenlinux.display;

import android.os.Handler;
import android.os.Looper;
import android.util.Log;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;

/**
 * Manages the TCP connection to the Linux screen server.
 *
 * Each call to {@link #connect} starts a {@link Session} that owns a receive
 * thread (frames) and a send thread (input events), and retries with
 * exponential backoff until {@link #disconnect} is called. Network I/O never
 * happens on the UI thread; {@link #send} only enqueues.
 *
 * Wire protocol: see server/screen_server.py.
 */
public class ScreenClient {

    private static final String TAG = "ScreenClient";
    private static final int CONNECT_TIMEOUT_MS = 3000;
    private static final int SOCKET_TIMEOUT_MS = 10000; // server sends a keep-alive frame every 1 s
    private static final int INITIAL_RECONNECT_DELAY_MS = 500;
    private static final int MAX_RECONNECT_DELAY_MS = 8000;
    private static final int RECEIVE_BUFFER_SIZE = 1024 * 1024;
    private static final int MAX_FRAME_SIZE = 32 * 1024 * 1024;
    private static final long FPS_UPDATE_INTERVAL_MS = 500;
    private static final int MAX_QUEUED_MESSAGES = 512;
    /** Sent first so the server knows this is the app, not a browser: "SRLN" + version 1. */
    private static final byte[] HELLO = {'S', 'R', 'L', 'N', 0, 1};

    /** Callbacks, always delivered on the UI thread and only for the current session. */
    public interface ConnectionListener {
        void onConnecting(int attempt);
        void onConnected();
        /** @param retryInMs delay before the next attempt */
        void onConnectionLost(String reason, int retryInMs);
        void onFpsUpdate(float fps);
    }

    private final FrameRenderer renderer;
    private final ConnectionListener listener;
    private final Handler uiHandler = new Handler(Looper.getMainLooper());

    private volatile Session session;

    public ScreenClient(FrameRenderer renderer, ConnectionListener listener) {
        this.renderer = renderer;
        this.listener = listener;
    }

    /** True while a session exists (connecting, connected or waiting to retry). */
    public boolean isActive() {
        return session != null;
    }

    public boolean isConnected() {
        Session s = session;
        return s != null && s.connected;
    }

    /** Start connecting; no-op if already active. */
    public void connect(String host, int port) {
        if (session != null) {
            return;
        }
        Session s = new Session(host, port);
        session = s;
        s.start();
    }

    /** Stop the current session. No further callbacks are delivered for it. */
    public void disconnect() {
        Session s = session;
        session = null;
        if (s != null) {
            s.cancel();
        }
    }

    /** Queue a 6-byte protocol message for sending. Safe to call from any thread. */
    public void send(byte[] message) {
        Session s = session;
        if (s != null && s.connected) {
            s.outQueue.offer(message);
        }
    }

    // ------------------------------------------------------------------------

    private final class Session {
        final String host;
        final int port;
        final LinkedBlockingQueue<byte[]> outQueue =
                new LinkedBlockingQueue<>(MAX_QUEUED_MESSAGES);

        volatile boolean cancelled;
        volatile boolean connected;
        volatile Socket socket;
        volatile Thread senderThread;
        Thread receiverThread;
        long lastFpsUpdate;

        Session(String host, int port) {
            this.host = host;
            this.port = port;
        }

        void start() {
            receiverThread = new Thread(this::run, "SreenLinux-Receiver");
            receiverThread.setDaemon(true);
            receiverThread.start();
        }

        void cancel() {
            cancelled = true;
            connected = false;
            receiverThread.interrupt();
            Thread sender = senderThread;
            if (sender != null) {
                sender.interrupt();
            }
            // Closing unblocks the receiver's read(). Socket.close() on a
            // non-lingering socket is allowed on the UI thread, but stay safe.
            final Socket s = socket;
            if (s != null) {
                new Thread(() -> closeQuietly(s), "SreenLinux-Close").start();
            }
        }

        /** Post a callback, dropping it if this session is no longer current. */
        void post(Runnable r) {
            uiHandler.post(() -> {
                if (session == this && !cancelled) {
                    r.run();
                }
            });
        }

        void run() {
            int delay = INITIAL_RECONNECT_DELAY_MS;
            int attempt = 0;
            while (!cancelled) {
                attempt++;
                final int a = attempt;
                post(() -> listener.onConnecting(a));

                String reason;
                boolean wasConnected = false;
                Socket s = new Socket();
                socket = s;
                try {
                    s.setTcpNoDelay(true);
                    s.setReceiveBufferSize(RECEIVE_BUFFER_SIZE);
                    s.connect(new InetSocketAddress(host, port), CONNECT_TIMEOUT_MS);
                    s.setSoTimeout(SOCKET_TIMEOUT_MS);
                    if (cancelled) {
                        break;
                    }
                    InputStream in = new BufferedInputStream(s.getInputStream(), RECEIVE_BUFFER_SIZE);
                    OutputStream out = new BufferedOutputStream(s.getOutputStream(), 1024);

                    outQueue.clear();
                    renderer.resetStats();
                    out.write(HELLO);
                    out.flush();

                    // With `adb reverse` the TCP connect always succeeds (adbd
                    // accepts it) even if the server isn't running, so only
                    // treat the link as up once the first frame arrives.
                    receiveFirstFrame(in);
                    connected = true;
                    wasConnected = true;
                    startSender(out);
                    post(listener::onConnected);
                    attempt = 0;
                    delay = INITIAL_RECONNECT_DELAY_MS;

                    receiveFrames(in);
                    reason = "Server closed the connection";
                } catch (IOException e) {
                    reason = describe(e, wasConnected);
                    Log.w(TAG, "Connection error: " + e);
                } finally {
                    connected = false;
                    Thread sender = senderThread;
                    if (sender != null) {
                        sender.interrupt();
                        senderThread = null;
                    }
                    closeQuietly(s);
                    socket = null;
                }

                if (cancelled) {
                    break;
                }
                final String r = reason;
                final int d = delay;
                post(() -> listener.onConnectionLost(r, d));
                try {
                    Thread.sleep(delay);
                } catch (InterruptedException e) {
                    break;
                }
                delay = Math.min(delay * 2, MAX_RECONNECT_DELAY_MS);
            }
        }

        private void startSender(final OutputStream out) {
            Thread t = new Thread(() -> {
                try {
                    while (!cancelled && connected) {
                        byte[] msg = outQueue.poll(500, TimeUnit.MILLISECONDS);
                        if (msg == null) {
                            continue;
                        }
                        out.write(msg);
                        // Coalesce whatever else is already queued into one flush
                        byte[] next;
                        while ((next = outQueue.poll()) != null) {
                            out.write(next);
                        }
                        out.flush();
                    }
                } catch (InterruptedException ignored) {
                } catch (IOException e) {
                    Log.w(TAG, "Send failed: " + e);
                    // Let the receiver notice and reconnect
                    closeQuietly(socket);
                }
            }, "SreenLinux-Sender");
            t.setDaemon(true);
            senderThread = t;
            t.start();
        }

        private final byte[] sizeBuffer = new byte[4];
        private byte[] frameBuffer = new byte[256 * 1024];

        private void receiveFirstFrame(InputStream in) throws IOException {
            try {
                readOneFrame(in);
            } catch (IOException e) {
                if (e instanceof java.net.SocketTimeoutException) {
                    throw e;
                }
                throw new java.net.ConnectException(e.getMessage());
            }
        }

        /** Protocol: [4 bytes frame_size BE][frame_size bytes JPEG], repeated. */
        private void receiveFrames(InputStream in) throws IOException {
            while (!cancelled) {
                readOneFrame(in);
            }
        }

        private void readOneFrame(InputStream in) throws IOException {
            readFully(in, sizeBuffer, 4);
            int frameSize = ((sizeBuffer[0] & 0xFF) << 24)
                    | ((sizeBuffer[1] & 0xFF) << 16)
                    | ((sizeBuffer[2] & 0xFF) << 8)
                    | (sizeBuffer[3] & 0xFF);
            if (frameSize <= 0 || frameSize > MAX_FRAME_SIZE) {
                throw new IOException("Invalid frame size " + frameSize
                        + " (is the port used by another program?)");
            }
            if (frameSize > frameBuffer.length) {
                frameBuffer = new byte[frameSize + frameSize / 4];
            }
            readFully(in, frameBuffer, frameSize);
            renderer.renderFrame(frameBuffer, frameSize);

            long now = System.currentTimeMillis();
            if (now - lastFpsUpdate >= FPS_UPDATE_INTERVAL_MS) {
                lastFpsUpdate = now;
                final float fps = renderer.getCurrentFps();
                post(() -> listener.onFpsUpdate(fps));
            }
        }
    }

    // ------------------------------------------------------------------------

    private static void readFully(InputStream in, byte[] buffer, int count) throws IOException {
        int offset = 0;
        while (offset < count) {
            int read = in.read(buffer, offset, count - offset);
            if (read < 0) {
                throw new IOException("Server closed the connection");
            }
            offset += read;
        }
    }

    private static String describe(IOException e, boolean wasConnected) {
        if (e instanceof java.net.SocketTimeoutException) {
            return wasConnected ? "Server stopped responding" : "Connection timed out";
        }
        if (e instanceof java.net.ConnectException) {
            return "Server not reachable — is it running and is `adb reverse` set up?";
        }
        if (e instanceof java.net.UnknownHostException) {
            return "Unknown host";
        }
        String msg = e.getMessage();
        return wasConnected ? "Connection lost" : (msg != null ? msg : "Connection failed");
    }

    private static void closeQuietly(Socket s) {
        if (s == null) {
            return;
        }
        try {
            s.close();
        } catch (IOException ignored) {
        }
    }
}
