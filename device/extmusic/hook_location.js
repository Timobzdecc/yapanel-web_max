var LIB_GST = 'libquasar_gstreamer.so';
var logPath = '/data/data/com.yandex.io.sdk/files/gst_prop_log.txt';
var overridePath = '/data/data/com.yandex.io.sdk/files/override_url.txt';

var logFile = null;
try {
  logFile = new File(logPath, 'a');
} catch(e) {}

function writeLog(str) {
  if (logFile) {
    try {
      logFile.write(str);
      logFile.flush();
    } catch(e) {}
  }
}

function getOverrideUrl() {
  try {
    var f = new File(overridePath, 'r');
    var url = f.readText(65536).trim();
    f.close();
    if (url.length > 10) {
      return url;
    }
  } catch(e) {}
  return null;
}

var isAttached = false;

function attachHook(setPropAddr) {
  if (isAttached) return;
  isAttached = true;

  var g_value_get_string_addr = Module.findExportByName(LIB_GST, 'g_value_get_string');
  var g_value_set_string_addr = Module.findExportByName(LIB_GST, 'g_value_set_string');

  if (!g_value_get_string_addr || !g_value_set_string_addr) {
    writeLog('WARN: g_value string functions not found\n');
    return;
  }

  var g_value_get_string = new NativeFunction(g_value_get_string_addr, 'pointer', ['pointer']);
  var g_value_set_string = new NativeFunction(g_value_set_string_addr, 'void', ['pointer', 'pointer']);

  Interceptor.attach(setPropAddr, {
    onEnter: function(args) {
      try {
        var propNamePtr = args[1];
        if (!propNamePtr.isNull()) {
          var propName = propNamePtr.readUtf8String();
          if (propName === 'location' || propName === 'uri') {
            var valGVal = args[2];
            var origUrlPtr = g_value_get_string(valGVal);
            var origUrl = (!origUrlPtr.isNull()) ? origUrlPtr.readUtf8String() : '';
            var override = getOverrideUrl();

            if (override && origUrl.indexOf('googlevideo') === -1) {
              writeLog('[NATIVE_INJECT] Overriding location: ' + origUrl.substr(0, 50) + ' -> ' + override.substr(0, 60) + '...\n');
              var newStrPtr = Memory.allocUtf8String(override);
              g_value_set_string(valGVal, newStrPtr);
            } else {
              writeLog('[NATIVE_INJECT] Pass-through location -> ' + origUrl.substr(0, 60) + '...\n');
            }
          }
        }
      } catch(e) {
        writeLog('set_prop err: ' + e + '\n');
      }
    }
  });

  writeLog('NATIVE INJECTION HOOK ATTACHED\n');
}

function tryAttach() {
  var addr = Module.findExportByName(LIB_GST, 'g_object_set_property');
  if (addr) {
    attachHook(addr);
    return true;
  }
  return false;
}

if (!tryAttach()) {
  var intervalId = setInterval(function() {
    if (tryAttach()) {
      clearInterval(intervalId);
    }
  }, 250);
}
