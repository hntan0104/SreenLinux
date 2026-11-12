package com.sreenlinux.display;

import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.view.HapticFeedbackConstants;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewConfiguration;

/**
 * Turns touch gestures on the SurfaceView into mouse events for the server.
 *
 * Gestures:
 *   1-finger tap           → left click
 *   1-finger long press    → right click
 *   1-finger drag          → left-button drag (press, move, release)
 *   2-finger drag          → scroll (vertical and horizontal)
 *   2-finger tap           → right click
 *   3-finger tap           → {@link Listener#onThreeFingerTap()} (toggle overlay)
 *
 * Protocol messages are 6 bytes: [type][arg][x hi][x lo][y hi][y lo]
 *   type 1 touch  arg 0=down 1=move 2=up
 *   type 2 click  arg = button (1 left, 2 middle, 3 right)
 *   type 3 scroll arg 0=up 1=down 2=left 3=right
 * Coordinates are frame pixels.
 */
public class TouchHandler {

    public interface Sender {
        void send(byte[] message);
    }

    public interface Listener {
        void onThreeFingerTap();
    }

    static final byte MSG_TOUCH = 1;
    static final byte MSG_CLICK = 2;
    static final byte MSG_SCROLL = 3;

    static final int TOUCH_DOWN = 0;
    static final int TOUCH_MOVE = 1;
    static final int TOUCH_UP = 2;

    static final int BUTTON_LEFT = 1;
    static final int BUTTON_RIGHT = 3;

    static final int SCROLL_UP = 0;
    static final int SCROLL_DOWN = 1;
    static final int SCROLL_LEFT = 2;
    static final int SCROLL_RIGHT = 3;

    private static final long MULTI_TAP_TIMEOUT_MS = 300;

    private enum State { IDLE, PENDING, DRAGGING, SCROLLING, IGNORE }

    private final FrameRenderer renderer;
    private final Sender sender;
    private final Listener listener;
    private final View view;
    private final Handler handler = new Handler(Looper.getMainLooper());

    private final int touchSlop;
    private final long longPressMs;
    private final float scrollStepPx;

    private State state = State.IDLE;
    private float downX, downY;
    private float lastX, lastY;
    private long gestureStart;
    private int maxPointers;
    private boolean multiMoved;
    private float multiStartX, multiStartY;
    private float scrollLastX, scrollLastY;
    private float scrollAccX, scrollAccY;

    private final Runnable longPress = new Runnable() {
        @Override
        public void run() {
            if (state == State.PENDING) {
                sendFrame(MSG_CLICK, BUTTON_RIGHT, downX, downY);
                view.performHapticFeedback(HapticFeedbackConstants.LONG_PRESS);
                state = State.IGNORE;
            }
        }
    };

    public TouchHandler(Context context, View view, FrameRenderer renderer,
                        Sender sender, Listener listener) {
        this.view = view;
        this.renderer = renderer;
        this.sender = sender;
        this.listener = listener;
        ViewConfiguration vc = ViewConfiguration.get(context);
        this.touchSlop = vc.getScaledTouchSlop();
        this.longPressMs = ViewConfiguration.getLongPressTimeout();
        // One wheel "notch" per ~1/3 of a finger width of movement
        this.scrollStepPx = 12f * context.getResources().getDisplayMetrics().density;
    }

    /** Process a MotionEvent from the SurfaceView (UI thread). */
    public boolean onTouchEvent(MotionEvent ev) {
        switch (ev.getActionMasked()) {
            case MotionEvent.ACTION_DOWN:
                state = State.PENDING;
                downX = lastX = ev.getX();
                downY = lastY = ev.getY();
                gestureStart = ev.getEventTime();
                maxPointers = 1;
                multiMoved = false;
                handler.postDelayed(longPress, longPressMs);
                return true;

            case MotionEvent.ACTION_POINTER_DOWN: {
                int count = ev.getPointerCount();
                maxPointers = Math.max(maxPointers, count);
                handler.removeCallbacks(longPress);
                if (state == State.DRAGGING) {
                    // A second finger ends the drag
                    sendFrame(MSG_TOUCH, TOUCH_UP, lastX, lastY);
                    state = State.IGNORE;
                } else if (state == State.PENDING || state == State.SCROLLING) {
                    state = State.SCROLLING;
                    multiStartX = scrollLastX = centroidX(ev, -1);
                    multiStartY = scrollLastY = centroidY(ev, -1);
                    scrollAccX = scrollAccY = 0f;
                }
                return true;
            }

            case MotionEvent.ACTION_MOVE:
                handleMove(ev);
                return true;

            case MotionEvent.ACTION_POINTER_UP:
                if (state == State.SCROLLING) {
                    // Re-anchor on the remaining fingers to avoid a jump
                    int skip = ev.getActionIndex();
                    scrollLastX = centroidX(ev, skip);
                    scrollLastY = centroidY(ev, skip);
                }
                return true;

            case MotionEvent.ACTION_UP:
                handler.removeCallbacks(longPress);
                handleUp(ev);
                state = State.IDLE;
                return true;

            case MotionEvent.ACTION_CANCEL:
                handler.removeCallbacks(longPress);
                if (state == State.DRAGGING) {
                    sendFrame(MSG_TOUCH, TOUCH_UP, lastX, lastY);
                }
                state = State.IDLE;
                return true;

            default:
                return false;
        }
    }

    private void handleMove(MotionEvent ev) {
        float x = ev.getX(0);
        float y = ev.getY(0);
        switch (state) {
            case PENDING:
                if (Math.hypot(x - downX, y - downY) > touchSlop) {
                    handler.removeCallbacks(longPress);
                    state = State.DRAGGING;
                    sendFrame(MSG_TOUCH, TOUCH_DOWN, downX, downY);
                    sendFrame(MSG_TOUCH, TOUCH_MOVE, x, y);
                }
                break;
            case DRAGGING:
                if (x != lastX || y != lastY) {
                    sendFrame(MSG_TOUCH, TOUCH_MOVE, x, y);
                }
                break;
            case SCROLLING:
                if (ev.getPointerCount() >= 2) {
                    handleScroll(ev);
                }
                break;
            default:
                break;
        }
        lastX = x;
        lastY = y;
    }

    private void handleScroll(MotionEvent ev) {
        float cx = centroidX(ev, -1);
        float cy = centroidY(ev, -1);
        if (!multiMoved && Math.hypot(cx - multiStartX, cy - multiStartY) > touchSlop) {
            multiMoved = true;
        }
        if (maxPointers > 2) {
            return; // 3+ fingers: reserved for the overlay gesture
        }
        scrollAccX += cx - scrollLastX;
        scrollAccY += cy - scrollLastY;
        scrollLastX = cx;
        scrollLastY = cy;

        // "Natural" scrolling: content follows the fingers
        while (scrollAccY >= scrollStepPx) {
            sendFrame(MSG_SCROLL, SCROLL_UP, cx, cy);
            scrollAccY -= scrollStepPx;
        }
        while (scrollAccY <= -scrollStepPx) {
            sendFrame(MSG_SCROLL, SCROLL_DOWN, cx, cy);
            scrollAccY += scrollStepPx;
        }
        while (scrollAccX >= scrollStepPx) {
            sendFrame(MSG_SCROLL, SCROLL_LEFT, cx, cy);
            scrollAccX -= scrollStepPx;
        }
        while (scrollAccX <= -scrollStepPx) {
            sendFrame(MSG_SCROLL, SCROLL_RIGHT, cx, cy);
            scrollAccX += scrollStepPx;
        }
    }

    private void handleUp(MotionEvent ev) {
        float x = ev.getX();
        float y = ev.getY();
        long duration = ev.getEventTime() - gestureStart;
        switch (state) {
            case PENDING:
                sendFrame(MSG_CLICK, BUTTON_LEFT, downX, downY);
                break;
            case DRAGGING:
                sendFrame(MSG_TOUCH, TOUCH_UP, x, y);
                break;
            case SCROLLING:
                if (!multiMoved && duration < MULTI_TAP_TIMEOUT_MS + longPressMs / 2) {
                    if (maxPointers >= 3) {
                        listener.onThreeFingerTap();
                    } else if (maxPointers == 2) {
                        sendFrame(MSG_CLICK, BUTTON_RIGHT, downX, downY);
                    }
                }
                break;
            case IGNORE:
                if (maxPointers >= 3 && duration < MULTI_TAP_TIMEOUT_MS + longPressMs / 2) {
                    listener.onThreeFingerTap();
                }
                break;
            default:
                break;
        }
    }

    private static float centroidX(MotionEvent ev, int skipIndex) {
        float sum = 0f;
        int n = 0;
        for (int i = 0; i < ev.getPointerCount(); i++) {
            if (i == skipIndex) continue;
            sum += ev.getX(i);
            n++;
        }
        return n > 0 ? sum / n : 0f;
    }

    private static float centroidY(MotionEvent ev, int skipIndex) {
        float sum = 0f;
        int n = 0;
        for (int i = 0; i < ev.getPointerCount(); i++) {
            if (i == skipIndex) continue;
            sum += ev.getY(i);
            n++;
        }
        return n > 0 ? sum / n : 0f;
    }

    /** Map a view position to frame pixels and send one protocol message. */
    private void sendFrame(byte type, int arg, float viewX, float viewY) {
        FrameRenderer.Mapping m = renderer.getMapping();
        if (!m.isValid()) {
            return;
        }
        int x = m.toFrameX(viewX);
        int y = m.toFrameY(viewY);
        byte[] msg = new byte[6];
        msg[0] = type;
        msg[1] = (byte) arg;
        msg[2] = (byte) ((x >> 8) & 0xFF);
        msg[3] = (byte) (x & 0xFF);
        msg[4] = (byte) ((y >> 8) & 0xFF);
        msg[5] = (byte) (y & 0xFF);
        sender.send(msg);
    }

    /** Cancel any pending gesture (e.g. on disconnect). */
    public void reset() {
        handler.removeCallbacks(longPress);
        state = State.IDLE;
    }
}
