import android.content.Context;
import android.media.MediaDescription;
import android.media.MediaMetadata;
import android.media.Rating;
import android.media.session.MediaController;
import android.media.session.MediaSession;
import android.media.session.MediaSessionManager;
import android.media.session.PlaybackState;
import android.os.Looper;
import android.os.SystemClock;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.PrintWriter;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.List;

/**
 * Small control daemon for the native YouTube Music app.
 *
 * Runs through app_process as root in the shell SELinux domain: root passes
 * the MEDIA_CONTENT_CONTROL check, and the shell domain can reach the media
 * session service. The panel talks to it over a loopback socket.
 */
public final class NativeMediaCtl {
    private static final int DEFAULT_PORT = 8782;
    private static final String DEFAULT_PACKAGE = "app.rvx.android.apps.youtube.music";
    private static MediaSessionManager manager;
    private static String packageName = DEFAULT_PACKAGE;

    private NativeMediaCtl() {
    }

    public static void main(String[] args) {
        int port = DEFAULT_PORT;
        if (args.length > 0) {
            try {
                port = Integer.parseInt(args[0]);
            } catch (NumberFormatException ignored) {
            }
        }
        if (args.length > 1 && args[1] != null && args[1].length() > 0) {
            packageName = args[1];
        }
        try {
            Looper.prepareMainLooper();
            Class<?> activityThread = Class.forName("android.app.ActivityThread");
            Object thread = activityThread.getMethod("systemMain").invoke(null);
            Context context = (Context) activityThread.getMethod("getSystemContext").invoke(thread);
            manager = (MediaSessionManager) context.getSystemService(Context.MEDIA_SESSION_SERVICE);
            ServerSocket server = new ServerSocket(port, 16, InetAddress.getByName("127.0.0.1"));
            System.out.println("READY " + port);
            System.out.flush();
            while (true) {
                Socket socket = null;
                try {
                    socket = server.accept();
                    socket.setSoTimeout(30000);
                    handle(socket);
                } catch (Throwable ignored) {
                } finally {
                    if (socket != null) {
                        try {
                            socket.close();
                        } catch (Throwable ignored) {
                        }
                    }
                }
            }
        } catch (Throwable error) {
            System.out.println("FATAL " + error);
            error.printStackTrace(System.out);
            System.exit(1);
        }
    }

    private static void handle(Socket socket) throws Exception {
        BufferedReader in = new BufferedReader(new InputStreamReader(socket.getInputStream(), StandardCharsets.UTF_8));
        PrintWriter out = new PrintWriter(socket.getOutputStream(), true);
        String line;
        while ((line = in.readLine()) != null) {
            String response = execute(line.trim());
            out.println(response);
            out.flush();
            if (response.contains("\"quit\":true")) {
                return;
            }
        }
    }

    private static synchronized String execute(String command) {
        try {
            if (command.equals("PING") || command.equals("STATUS")) {
                return statusJson();
            }
            if (command.equals("QUEUE")) {
                return queueJson();
            }
            MediaController controller = controller();
            if (controller == null) {
                if (command.equals("QUIT")) {
                    return "{\"ok\":true,\"session\":false,\"quit\":true}";
                }
                return "{\"ok\":false,\"error\":\"no active YouTube Music session\"}";
            }
            MediaController.TransportControls controls = controller.getTransportControls();
            if (command.equals("PLAY")) {
                controls.play();
            } else if (command.equals("PAUSE")) {
                controls.pause();
            } else if (command.equals("TOGGLE")) {
                PlaybackState state = controller.getPlaybackState();
                if (state != null && state.getState() == PlaybackState.STATE_PLAYING) {
                    controls.pause();
                } else {
                    controls.play();
                }
            } else if (command.equals("NEXT")) {
                controls.skipToNext();
            } else if (command.equals("PREV")) {
                controls.skipToPrevious();
            } else if (command.equals("STOP")) {
                controls.stop();
            } else if (command.startsWith("SEEK ")) {
                controls.seekTo(Long.parseLong(command.substring(5).trim()));
            } else if (command.startsWith("SKIP ")) {
                controls.skipToQueueItem(Long.parseLong(command.substring(5).trim()));
            } else if (command.startsWith("SEEKREL ")) {
                PlaybackState state = controller.getPlaybackState();
                long current = state == null ? 0 : state.getPosition();
                long delta = Long.parseLong(command.substring(8).trim());
                controls.seekTo(Math.max(0, current + delta));
            } else if (command.startsWith("RATING ")) {
                String value = command.substring(7).trim();
                if (value.equals("like")) {
                    controls.setRating(Rating.newThumbRating(true));
                } else if (value.equals("dislike")) {
                    controls.setRating(Rating.newThumbRating(false));
                } else {
                    controls.setRating(Rating.newHeartRating(false));
                }
            } else if (command.equals("QUIT")) {
                return "{\"ok\":true,\"quit\":true}";
            } else {
                return "{\"ok\":false,\"error\":\"unknown command\"}";
            }
            return statusJson();
        } catch (Throwable error) {
            return "{\"ok\":false,\"error\":\"" + escape(String.valueOf(error)) + "\"}";
        }
    }

    private static MediaController controller() {
        if (manager == null) {
            return null;
        }
        List<MediaController> sessions = manager.getActiveSessions(null);
        if (sessions == null) {
            return null;
        }
        MediaController best = null;
        for (MediaController item : sessions) {
            if (!packageName.equals(String.valueOf(item.getPackageName()))) {
                continue;
            }
            if (best == null) {
                best = item;
            }
            try {
                PlaybackState state = item.getPlaybackState();
                int code = state == null ? 0 : state.getState();
                if (code == PlaybackState.STATE_PLAYING
                        || code == PlaybackState.STATE_PAUSED
                        || code == PlaybackState.STATE_BUFFERING) {
                    return item;
                }
            } catch (Throwable ignored) {
            }
        }
        return best;
    }

    private static String queueJson() {
        try {
            MediaController controller = controller();
            if (controller == null) {
                return "{\"ok\":true,\"active_id\":-1,\"items\":[]}";
            }
            PlaybackState state = controller.getPlaybackState();
            long active = state == null ? -1 : state.getActiveQueueItemId();
            List<MediaSession.QueueItem> queue = controller.getQueue();
            StringBuilder out = new StringBuilder(256 + (queue == null ? 0 : queue.size() * 120));
            out.append("{\"ok\":true,\"active_id\":").append(active).append(",\"items\":[");
            if (queue != null) {
                int count = 0;
                for (MediaSession.QueueItem item : queue) {
                    if (count >= 200) {
                        break;
                    }
                    if (count > 0) {
                        out.append(',');
                    }
                    MediaDescription description = item.getDescription();
                    String title = description == null ? "" : string(description.getTitle());
                    String artist = description == null ? "" : string(description.getSubtitle());
                    long duration = 0;
                    if (description != null && description.getExtras() != null) {
                        duration = description.getExtras().getLong(MediaMetadata.METADATA_KEY_DURATION, 0);
                    }
                    out.append("{\"queue_id\":").append(item.getQueueId())
                            .append(",\"title\":\"").append(escape(title))
                            .append("\",\"artist\":\"").append(escape(artist))
                            .append("\",\"duration\":").append(Math.max(0, duration)).append('}');
                    count++;
                }
            }
            out.append("]}");
            return out.toString();
        } catch (Throwable error) {
            return "{\"ok\":false,\"error\":\"" + escape(String.valueOf(error)) + "\"}";
        }
    }

    private static String statusJson() {
        try {
            MediaController controller = controller();
            if (controller == null) {
                return "{\"ok\":true,\"available\":true,\"session\":false,\"state\":0,"
                        + "\"state_name\":\"none\",\"position\":0,\"duration\":0,\"speed\":0,"
                        + "\"playing\":false,\"paused\":false,\"title\":\"\",\"artist\":\"\","
                        + "\"album\":\"\",\"media_id\":\"\",\"queue_size\":0}";
            }
            PlaybackState state = controller.getPlaybackState();
            MediaMetadata metadata = controller.getMetadata();
            int code = state == null ? 0 : state.getState();
            long position = state == null ? 0 : state.getPosition();
            float speed = state == null ? 0 : state.getPlaybackSpeed();
            if (state != null && code == PlaybackState.STATE_PLAYING) {
                position += Math.max(0, SystemClock.elapsedRealtime() - state.getLastPositionUpdateTime()) * speed;
            }
            long duration = metadata == null ? 0 : metadata.getLong(MediaMetadata.METADATA_KEY_DURATION);
            if (duration > 0 && position > duration) {
                position = duration;
            }
            String title = metadata == null ? "" : string(metadata.getString(MediaMetadata.METADATA_KEY_TITLE));
            String artist = metadata == null ? "" : string(metadata.getString(MediaMetadata.METADATA_KEY_ARTIST));
            String album = metadata == null ? "" : string(metadata.getString(MediaMetadata.METADATA_KEY_ALBUM));
            String mediaId = metadata == null ? "" : string(metadata.getString(MediaMetadata.METADATA_KEY_MEDIA_ID));
            int queueSize = 0;
            try {
                List<MediaSession.QueueItem> queue = controller.getQueue();
                queueSize = queue == null ? 0 : queue.size();
            } catch (Throwable ignored) {
            }
            return "{\"ok\":true,\"available\":true,\"session\":true,"
                    + "\"state\":" + code + ",\"state_name\":\"" + stateName(code) + "\","
                    + "\"position\":" + Math.max(0, position) + ",\"duration\":" + Math.max(0, duration) + ","
                    + "\"speed\":" + speed + ",\"playing\":" + (code == PlaybackState.STATE_PLAYING) + ","
                    + "\"paused\":" + (code == PlaybackState.STATE_PAUSED) + ","
                    + "\"title\":\"" + escape(title) + "\",\"artist\":\"" + escape(artist) + "\","
                    + "\"album\":\"" + escape(album) + "\",\"media_id\":\"" + escape(mediaId) + "\","
                    + "\"queue_size\":" + queueSize + "}";
        } catch (Throwable error) {
            return "{\"ok\":false,\"error\":\"" + escape(String.valueOf(error)) + "\"}";
        }
    }

    private static String stateName(int code) {
        switch (code) {
            case PlaybackState.STATE_STOPPED:
                return "stopped";
            case PlaybackState.STATE_PAUSED:
                return "paused";
            case PlaybackState.STATE_PLAYING:
                return "playing";
            case PlaybackState.STATE_FAST_FORWARDING:
                return "fast_forward";
            case PlaybackState.STATE_REWINDING:
                return "rewind";
            case PlaybackState.STATE_BUFFERING:
                return "buffering";
            case PlaybackState.STATE_ERROR:
                return "error";
            case PlaybackState.STATE_CONNECTING:
                return "connecting";
            case PlaybackState.STATE_SKIPPING_TO_NEXT:
            case PlaybackState.STATE_SKIPPING_TO_PREVIOUS:
                return "skipping";
            case PlaybackState.STATE_SKIPPING_TO_QUEUE_ITEM:
                return "skipping";
            default:
                return "none";
        }
    }

    private static String string(Object value) {
        return value == null ? "" : String.valueOf(value);
    }

    private static String escape(String value) {
        if (value == null) {
            return "";
        }
        StringBuilder out = new StringBuilder(value.length() + 16);
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            switch (c) {
                case '\\':
                    out.append("\\\\");
                    break;
                case '"':
                    out.append("\\\"");
                    break;
                case '\n':
                    out.append("\\n");
                    break;
                case '\r':
                    out.append("\\r");
                    break;
                case '\t':
                    out.append("\\t");
                    break;
                default:
                    if (c < 0x20) {
                        out.append(String.format("\\u%04x", (int) c));
                    } else {
                        out.append(c);
                    }
            }
        }
        return out.toString();
    }
}
