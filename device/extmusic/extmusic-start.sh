#!/system/bin/sh
# Started by the panel through start-stop-daemon. The SELinux transition is the
# same one used by /odm/bin/playaudio: MediaPlayer needs the audio service.
echo u:r:system_server:s0 > /proc/self/attr/current 2>/dev/null || true
exec env -i PATH=/system/bin:/system/xbin ANDROID_ROOT=/system ANDROID_DATA=/data \
  CLASSPATH=/data/yapanel-state/bin/ExtMusicPlayer.dex \
  app_process /system/bin ExtMusicPlayer 8781 >> /data/yapanel-state/extmusic.log 2>&1
