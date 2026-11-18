import android.media.AudioAttributes;
import android.media.MediaPlayer;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.PrintWriter;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.Base64;

/**
 * Small Android MediaPlayer daemon for external music sources.
 *
 * It listens only on 127.0.0.1 and speaks a line protocol:
 *   PLAY <base64url(http(s) url)>, PAUSE, RESUME, SEEK <ms>, STOP, STATUS, PING, QUIT
 * Responses are one-line JSON objects.
 *
 * The panel starts it through app_process and reaches it through adb forward.
 */
public final class ExtMusicPlayer {
    private static final int DEFAULT_PORT = 8781;

    private static final Object LOCK = new Object();
    private static MediaPlayer player;
    private static String state = "idle"; // idle, loading, playing, paused, ended, error
    private static String error = "";
    private static int duration;

    private ExtMusicPlayer() {
    }

    public static void main(String[] args) {
        int port = DEFAULT_PORT;
        if (args.length > 0) {
            try {
                port = Integer.parseInt(args[0]);
            } catch (NumberFormatException ignored) {
                // keep the default
            }
        }
        try {
            ServerSocket server = new ServerSocket(port, 16, InetAddress.getByName("127.0.0.1"));
            System.out.println("READY " + port);
            System.out.flush();
            acceptLoop(server);
        } catch (Exception e) {
            System.err.println("LISTEN " + e.getMessage());
            System.exit(1);
        }
    }

    private static void acceptLoop(ServerSocket server) {
        while (true) {
            Socket socket = null;
            try {
                socket = server.accept();
                socket.setSoTimeout(60000);
                handle(socket);
            } catch (Exception ignored) {
                // A single broken client must not kill the daemon.
            } finally {
                if (socket != null) {
                    try {
                        socket.close();
                    } catch (Exception ignored) {
                    }
                }
            }
        }
    }

    private static void handle(Socket socket) throws Exception {
        BufferedReader in = new BufferedReader(new InputStreamReader(socket.getInputStream(), StandardCharsets.UTF_8));
        PrintWriter out = new PrintWriter(socket.getOutputStream(), true);
        String line;
        while ((line = in.readLine()) != null) {
            String command = line.trim();
            if (command.isEmpty()) {
                continue;
            }
            String response = execute(command);
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
                return statusJson(true, false);
            }
            if (command.equals("PAUSE")) {
                pause();
                return statusJson(true, false);
            }
            if (command.equals("RESUME")) {
                resume();
                return statusJson(true, false);
            }
            if (command.equals("STOP")) {
                stop();
                return statusJson(true, false);
            }
            if (command.equals("QUIT")) {
                stop();
                return statusJson(true, true);
            }
            if (command.startsWith("SEEK ")) {
                seek(Integer.parseInt(command.substring(5).trim()));
                return statusJson(true, false);
            }
            if (command.startsWith("PLAY ")) {
                play(decodeUrl(command.substring(5).trim()));
                return statusJson(true, false);
            }
            return errorJson("unknown command");
        } catch (Exception e) {
            error = e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage();
            state = "error";
            return errorJson(error);
        }
    }

    private static String decodeUrl(String value) {
        byte[] raw = Base64.getUrlDecoder().decode(value);
        return new String(raw, StandardCharsets.UTF_8);
    }

    private static void play(String url) throws Exception {
        if (!(url.startsWith("http://") || url.startsWith("https://"))) {
            throw new IllegalArgumentException("only http(s) urls are supported");
        }
        stopLocked();
        state = "loading";
        error = "";
        duration = 0;
        MediaPlayer next = new MediaPlayer();
        next.setAudioAttributes(new AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                .build());
        next.setDataSource(url);
        // app_process has no prepared Looper, so prepareAsync callbacks never
        // arrive. prepare() is blocking but the URL is an HTTP stream: it only
        // buffers the beginning, it does not download the whole track.
        next.prepare();
        next.start();
        synchronized (LOCK) {
            player = next;
            state = "playing";
            duration = safeDuration(next);
        }
    }

    private static void pause() {
        synchronized (LOCK) {
            if (player != null && "playing".equals(state)) {
                try {
                    player.pause();
                    state = "paused";
                } catch (Exception e) {
                    setError(e);
                }
            }
        }
    }

    private static void resume() {
        synchronized (LOCK) {
            if (player != null && "paused".equals(state)) {
                try {
                    player.start();
                    state = "playing";
                } catch (Exception e) {
                    setError(e);
                }
            }
        }
    }

    private static void seek(int ms) {
        synchronized (LOCK) {
            if (player != null) {
                try {
                    player.seekTo(Math.max(0, ms));
                } catch (Exception e) {
                    setError(e);
                }
            }
        }
    }

    private static void stop() {
        synchronized (LOCK) {
            stopLocked();
            state = "idle";
            error = "";
            duration = 0;
        }
    }

    private static void stopLocked() {
        MediaPlayer old = player;
        player = null;
        if (old != null) {
            try {
                old.stop();
            } catch (Exception ignored) {
            }
            try {
                old.release();
            } catch (Exception ignored) {
            }
        }
    }

    private static int safeDuration(MediaPlayer mp) {
        try {
            return Math.max(0, mp.getDuration());
        } catch (Exception ignored) {
            return 0;
        }
    }

    private static void setError(Exception e) {
        error = e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage();
        state = "error";
    }

    private static String statusJson(boolean ok, boolean quit) {
        synchronized (LOCK) {
            int position = 0;
            int total = duration;
            boolean playing = false;
            if (player != null) {
                try {
                    position = Math.max(0, player.getCurrentPosition());
                    total = safeDuration(player);
                    playing = player.isPlaying();
                } catch (Exception ignored) {
                }
            }
            if (player != null && "playing".equals(state) && !playing && total > 0 && position >= total - 400) {
                state = "ended";
            }
            return "{\"ok\":" + ok
                    + ",\"state\":\"" + esc(state) + "\""
                    + ",\"position\":" + position
                    + ",\"duration\":" + total
                    + ",\"playing\":" + playing
                    + ",\"error\":\"" + esc(error) + "\""
                    + ",\"quit\":" + quit
                    + "}";
        }
    }

    private static String errorJson(String message) {
        return "{\"ok\":false,\"state\":\"error\",\"position\":0,\"duration\":0,\"playing\":false,"
                + "\"error\":\"" + esc(message) + "\",\"quit\":false}";
    }

    private static String esc(String value) {
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
