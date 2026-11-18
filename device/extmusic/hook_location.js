var LIB_GST = 'libquasar_gstreamer.so';
var logPath = '/data/data/com.yandex.io.sdk/files/gst_prop_log.txt';
var overridePath = '/data/data/com.yandex.io.sdk/files/override_url.txt';
var userSeekFlagPath = '/data/data/com.yandex.io.sdk/files/user_seek.txt';

function writeLog(str) {
  try {
    var f = new File(logPath, 'a');
    f.write(str);
    f.close();
  } catch (e) {}
}

function getOverrideUrl() {
  try {
    var f = new File(overridePath, 'r');
    var url = f.readText(65536).trim();
    f.close();
    if (url.length > 10) {
      return url;
    }
  } catch (e) {}
  return null;
}

function checkAndClearUserSeekFlag() {
  try {
    var f = new File(userSeekFlagPath, 'r');
    var content = f.readText(16);
    f.close();
    if (content && content.indexOf('1') !== -1) {
      try {
        var fw = new File(userSeekFlagPath, 'w');
        fw.write('0');
        fw.close();
      } catch (e) {}
      return true;
    }
  } catch (e) {}
  return false;
}

var isAttached = false;

function attachHooks() {
  if (isAttached) return true;

  var parseLaunchAddr = Module.findExportByName(LIB_GST, 'gst_parse_launch');
  var seekAddr = Module.findExportByName(LIB_GST, 'gst_element_seek');
  var seekSimpleAddr = Module.findExportByName(LIB_GST, 'gst_element_seek_simple');

  if (!parseLaunchAddr) {
    return false;
  }
  isAttached = true;

  // 1. Hook gst_parse_launch: replace stream location and strip aesctrdec for external URLs
  Interceptor.attach(parseLaunchAddr, {
    onEnter: function(args) {
      try {
        if (args[0].isNull()) return;
        var str = args[0].readUtf8String();
        var override = getOverrideUrl();

        if (override && str.indexOf('yio_souphttpsrc') !== -1) {
          var modified = str;
          modified = modified.replace(/alt-locations="[^"]*"\s*/g, '');
          modified = modified.replace(/location="[^"]*"/g, 'location="' + override + '"');
          modified = modified.replace(/!\s*aesctrdec\s+key=[0-9a-fA-F]+\s*!/g, '!');
          writeLog('[GST_PARSE_LAUNCH] External URL -> ' + override.substr(0, 120) + '\n');
          this.pipePtr = Memory.allocUtf8String(modified);
          args[0] = this.pipePtr;
        } else if (override && str.indexOf('filesrc') !== -1) {
          var modFile = str.replace(
            /filesrc\s+location="[^"]*"\s*!\s*aesctrdec\s+key=[0-9a-fA-F]+\s*!/g,
            'souphttpsrc location="' + override + '" name=yio_souphttpsrc !'
          );
          writeLog('[GST_PARSE_LAUNCH] External filesrc -> ' + override.substr(0, 120) + '\n');
          this.pipePtr = Memory.allocUtf8String(modFile);
          args[0] = this.pipePtr;
        }
      } catch (e) {
        writeLog('[GST_PARSE_LAUNCH] Error: ' + e + '\n');
      }
    }
  });

  // 2. Hook gst_element_seek: absorb Yandex startup seeks on external tracks; allow explicit fallback user seeks
  if (seekAddr && seekSimpleAddr) {
    var realSeekSimple = new NativeFunction(seekSimpleAddr, 'int', ['pointer', 'int', 'int', 'int64']);
    Interceptor.replace(seekAddr, new NativeCallback(function(elem, rate, format, flags, startType, startPos, stopType, stopPos) {
      var override = getOverrideUrl();
      if (override) {
        if (!checkAndClearUserSeekFlag()) {
          writeLog('[GST_SEEK] Absorbed internal seek startPos=' + startPos + '\n');
          return 1;
        }
        writeLog('[GST_SEEK] User seek startPos=' + startPos + '\n');
        var res = realSeekSimple(elem, 3, 5, startPos);
        if (res === 0) {
          res = realSeekSimple(elem, 3, 1, startPos);
        }
        writeLog('[GST_SEEK] realSeekSimple returned ' + res + '\n');
        return res !== 0 ? 1 : 0;
      }
      return realSeekSimple(elem, format, flags, startPos);
    }, 'int', ['pointer', 'double', 'int', 'int', 'int', 'int64', 'int', 'int64']));
    writeLog('[GST_SEEK] Hooked gst_element_seek via realSeekSimple\n');
  }

  writeLog('NATIVE INJECTION HOOKS ATTACHED\n');
  return true;
}

if (!attachHooks()) {
  var checkTimer = setInterval(function() {
    if (attachHooks()) {
      clearInterval(checkTimer);
    }
  }, 500);
}
