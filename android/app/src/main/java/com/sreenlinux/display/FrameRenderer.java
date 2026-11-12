package com.sreenlinux.display;

import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.RectF;
import android.view.SurfaceHolder;

/**
 * Decodes JPEG frames and renders them onto a SurfaceView.
 * Handles aspect-correct scaling (letterboxing), FPS tracking, and bitmap reuse.
 *
 * Threading: {@link #renderFrame} runs on the network thread; {@link #setDisplaySize},
 * {@link #redraw} and {@link #getMapping} are called from the UI thread.
 */
public class FrameRenderer {

    /**
     * Immutable snapshot of how frame pixels map onto the view, so the touch
     * handler always sees a consistent set of values.
     */
    public static final class Mapping {
        public final int frameWidth;
        public final int frameHeight;
        public final float offsetX;
        public final float offsetY;
        public final float scale;

        Mapping(int frameWidth, int frameHeight, float offsetX, float offsetY, float scale) {
            this.frameWidth = frameWidth;
            this.frameHeight = frameHeight;
            this.offsetX = offsetX;
            this.offsetY = offsetY;
            this.scale = scale;
        }

        public boolean isValid() {
            return frameWidth > 0 && frameHeight > 0 && scale > 0f;
        }

        /** View X → frame X, clamped to the frame. */
        public int toFrameX(float viewX) {
            int x = Math.round((viewX - offsetX) / scale);
            return Math.max(0, Math.min(x, frameWidth - 1));
        }

        /** View Y → frame Y, clamped to the frame. */
        public int toFrameY(float viewY) {
            int y = Math.round((viewY - offsetY) / scale);
            return Math.max(0, Math.min(y, frameHeight - 1));
        }
    }

    private final SurfaceHolder surfaceHolder;
    private final Paint paint;
    private final BitmapFactory.Options decodeOptions;
    private final Object lock = new Object();

    // Guarded by lock
    private int frameWidth;
    private int frameHeight;
    private int displayWidth;
    private int displayHeight;
    private Bitmap frontBuffer;
    private Bitmap backBuffer;
    private boolean released;
    private final RectF destRect = new RectF();

    private volatile Mapping mapping = new Mapping(0, 0, 0f, 0f, 0f);

    // FPS tracking (network thread only)
    private long frameCount;
    private long fpsStartTime;
    private volatile float currentFps;

    public FrameRenderer(SurfaceHolder holder) {
        this.surfaceHolder = holder;
        this.paint = new Paint(Paint.FILTER_BITMAP_FLAG);
        this.decodeOptions = new BitmapFactory.Options();
        this.decodeOptions.inMutable = true;
        this.decodeOptions.inPreferredConfig = Bitmap.Config.RGB_565; // half the memory of ARGB
        this.fpsStartTime = System.nanoTime();
    }

    /**
     * Decode JPEG data and render it to the surface (network thread).
     */
    public void renderFrame(byte[] jpegData, int length) {
        synchronized (lock) {
            if (released) {
                return;
            }
            Bitmap decoded = null;
            decodeOptions.inBitmap = backBuffer;
            try {
                decoded = BitmapFactory.decodeByteArray(jpegData, 0, length, decodeOptions);
            } catch (IllegalArgumentException e) {
                // inBitmap can't be reused (frame size changed) — decode fresh
                decodeOptions.inBitmap = null;
                decoded = BitmapFactory.decodeByteArray(jpegData, 0, length, decodeOptions);
            }
            decodeOptions.inBitmap = null;
            if (decoded == null) {
                return; // corrupt frame — skip
            }

            if (frameWidth != decoded.getWidth() || frameHeight != decoded.getHeight()) {
                frameWidth = decoded.getWidth();
                frameHeight = decoded.getHeight();
                updateScalingLocked();
            }

            // Swap: the old front buffer becomes the next decode target
            backBuffer = frontBuffer;
            frontBuffer = decoded;
        }

        drawToSurface();
        trackFps();
    }

    /** Re-draw the last frame (e.g. after the surface was re-created). */
    public void redraw() {
        drawToSurface();
    }

    private void drawToSurface() {
        Canvas canvas = null;
        try {
            canvas = surfaceHolder.lockCanvas();
            if (canvas == null) {
                return; // surface not ready / destroyed
            }
            canvas.drawColor(Color.BLACK);
            synchronized (lock) {
                if (frontBuffer != null && !frontBuffer.isRecycled()) {
                    canvas.drawBitmap(frontBuffer, null, destRect, paint);
                }
            }
        } catch (Exception e) {
            // Surface destroyed between checks — ignore, the next frame will retry
        } finally {
            if (canvas != null) {
                try {
                    surfaceHolder.unlockCanvasAndPost(canvas);
                } catch (Exception ignored) {
                }
            }
        }
    }

    private void trackFps() {
        frameCount++;
        long now = System.nanoTime();
        long elapsed = now - fpsStartTime;
        if (elapsed >= 1_000_000_000L) {
            currentFps = (float) frameCount * 1_000_000_000L / elapsed;
            frameCount = 0;
            fpsStartTime = now;
        }
    }

    /** Reset FPS statistics (e.g. on a new connection). */
    public void resetStats() {
        frameCount = 0;
        fpsStartTime = System.nanoTime();
        currentFps = 0f;
    }

    /** Update the surface size and recalculate scaling (UI thread). */
    public void setDisplaySize(int width, int height) {
        synchronized (lock) {
            displayWidth = width;
            displayHeight = height;
            updateScalingLocked();
        }
    }

    /** Fit the frame inside the display, preserving aspect ratio. */
    private void updateScalingLocked() {
        if (frameWidth <= 0 || frameHeight <= 0 || displayWidth <= 0 || displayHeight <= 0) {
            return;
        }
        float scale = Math.min((float) displayWidth / frameWidth,
                               (float) displayHeight / frameHeight);
        float drawWidth = frameWidth * scale;
        float drawHeight = frameHeight * scale;
        float offsetX = (displayWidth - drawWidth) / 2f;
        float offsetY = (displayHeight - drawHeight) / 2f;
        destRect.set(offsetX, offsetY, offsetX + drawWidth, offsetY + drawHeight);
        mapping = new Mapping(frameWidth, frameHeight, offsetX, offsetY, scale);
    }

    public Mapping getMapping() {
        return mapping;
    }

    public float getCurrentFps() {
        return currentFps;
    }

    /** Release bitmap resources. The renderer ignores frames afterwards. */
    public void release() {
        synchronized (lock) {
            released = true;
            if (frontBuffer != null) {
                frontBuffer.recycle();
                frontBuffer = null;
            }
            if (backBuffer != null) {
                backBuffer.recycle();
                backBuffer = null;
            }
        }
    }
}
