import android.system.Os;

/**
 * Type text as the Android shell user.
 *
 * Android's `input text` sends no characters when the caller is root, even in
 * the shell SELinux domain. This helper starts in that domain, drops to uid
 * 2000 and execs the stock input command, matching an adb shell.
 */
public final class InputText {
    private InputText() {
    }

    public static void main(String[] args) {
        try {
            Os.setgid(2000);
            Os.setuid(2000);
            if (args.length > 0 && args[0].equals("--dump")) {
                Process dump = Runtime.getRuntime().exec(new String[]{
                        "/system/bin/uiautomator", "dump", "/data/local/tmp/panel-ui.xml"
                });
                dump.waitFor();
                return;
            }
            String text = args.length > 0 ? args[0] : "";
            Process process = Runtime.getRuntime().exec(new String[]{"/system/bin/input", "text", text});
            process.waitFor();
        } catch (Throwable error) {
            System.err.println("INPUT_TEXT " + error);
            System.exit(1);
        }
    }
}
