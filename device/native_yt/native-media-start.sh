#!/system/bin/sh
# Started by the panel as root. runcon switches to the shell domain, which can
# reach the media_session service; root uid passes its permission check.
D=/data/yapanel-state/bin
exec /system/bin/runcon u:r:shell:s0 /system/bin/env -i \
  PATH=/system/bin:/system/xbin \
  ANDROID_ROOT=/system \
  ANDROID_DATA=/data \
  CLASSPATH="$D/NativeMediaCtl.dex" \
  /system/bin/app_process /system/bin NativeMediaCtl 8782 app.rvx.android.apps.youtube.music \
  >> /data/yapanel-state/native-media.log 2>&1
