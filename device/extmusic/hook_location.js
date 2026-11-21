var LIB_GST = 'libquasar_gstreamer.so';
var logPath = '/data/data/com.yandex.io.sdk/files/gst_prop_log.txt';
var overridePath = '/data/data/com.yandex.io.sdk/files/override_url.txt';
var userSeekFlagPath = '/data/data/com.yandex.io.sdk/files/user_seek.txt';
var cancelPath = '/data/data/com.yandex.io.sdk/files/alice_cancel.txt';

function writeLog(str) {
  try {
    var f = new File(logPath, 'a');
    f.write(str);
    f.close();
  } catch (e) {}
}

function getHookState() {
  var root = typeof globalThis !== 'undefined' ? globalThis : this;
  if (!root.__yapanelHookState) {
    root.__yapanelHookState = {
      isAttached: false,
      attachTimer: null,
      cancelTimer: null,
      lastCancelSeq: 0,
      lastConsumedOverride: '',
      ttsMuteUntil: 0,
      musicMuteUntil: 0,
      sdkInst: null,
      onParseLaunch: null,
      onSeek: null
    };
  } else if (root.__yapanelHookState.lastConsumedOverride === undefined) {
    root.__yapanelHookState.lastConsumedOverride = '';
  }
  return root.__yapanelHookState;
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

function mutePipeline(str) {
  if (/volume\s+name=volume0\b/.test(str)) {
    return str.replace(/volume\s+name=volume0\b/, 'volume name=volume0 ! volume volume=0.0 mute=true');
  }
  if (/openslessink\b/.test(str)) {
    return str.replace(/openslessink\b/, 'volume volume=0.0 mute=true ! openslessink');
  }
  return str;
}

function extractNormGain(url) {
  if (!url) return null;
  var m = /[?&]norm=([0-9]+(?:\.[0-9]+)?)/.exec(url);
  if (!m) return null;
  var g = parseFloat(m[1]);
  if (!isFinite(g) || g < 0.05 || g > 3.0) return null;
  return g;
}

function applyNormGain(str, gain) {
  if (gain === null || gain === undefined) {
    return str;
  }
  var gStr = gain.toFixed(4);
  if (/volume\s+name=normalization\b[^!]*/.test(str)) {
    return str.replace(/volume\s+name=normalization\b[^!]*/, 'volume name=normalization volume=' + gStr + ' ');
  }
  if (/volume\s+name=volume0\b/.test(str)) {
    return str.replace(/volume\s+name=volume0\b/, 'volume name=normalization volume=' + gStr + ' ! volume name=volume0');
  }
  return str;
}

function invokeStopOnSdk(inst) {
  try { inst.stopTts(); } catch (e) {}
  try { inst.cancelDialog(); } catch (e) {}
  try { inst.stopConversation(); } catch (e) {}
}

function stopAliceNative() {
  var state = getHookState();
  try {
    Java.perform(function () {
      if (state.sdkInst) {
        invokeStopOnSdk(state.sdkInst);
        return;
      }
      Java.choose('ru.yandex.io.sdk.jni.YandexIOSDKImpl', {
        onMatch: function (inst) {
          if (!state.sdkInst) {
            state.sdkInst = inst;
            invokeStopOnSdk(inst);
          }
        },
        onComplete: function () {}
      });
    });
  } catch (e) {
    writeLog('[ALICE_CANCEL] Error: ' + e + '\n');
  }
}

function readCancelState() {
  var state = getHookState();
  try {
    var f = new File(cancelPath, 'r');
    var raw = f.readText(256).trim();
    f.close();
    if (!raw) {
      return;
    }
    var parts = raw.split('|');
    var seq = parseInt(parts[0] || '0', 10) || 0;
    var ttsUntil = parseInt(parts[1] || '0', 10) || 0;
    var musicUntil = parseInt(parts[2] || '0', 10) || 0;
    state.ttsMuteUntil = ttsUntil;
    state.musicMuteUntil = musicUntil;
    if (seq > 0 && seq > state.lastCancelSeq) {
      state.lastCancelSeq = seq;
      writeLog('[ALICE_CANCEL] seq=' + seq + ' ttsUntil=' + ttsUntil + ' musicUntil=' + musicUntil + '\n');
      stopAliceNative();
      setTimeout(function () {
        if (state.lastCancelSeq === seq && Date.now() < state.ttsMuteUntil) {
          stopAliceNative();
        }
      }, 120);
      setTimeout(function () {
        if (state.lastCancelSeq === seq && Date.now() < state.ttsMuteUntil) {
          stopAliceNative();
        }
      }, 280);
    }
  } catch (e) {}
}

function handleParseLaunch(args) {
  var state = getHookState();
  try {
    if (args[0].isNull()) return;
    readCancelState();
    var str = args[0].readUtf8String();
    var now = Date.now();

    if (str.indexOf('stream-src') !== -1 && str.indexOf('opusdec') !== -1) {
      if (now < state.ttsMuteUntil) {
        var mutedTts = mutePipeline(str);
        writeLog('[GST_PARSE_LAUNCH] Muted native Alice TTS\n');
        this.pipePtr = Memory.allocUtf8String(mutedTts);
        args[0] = this.pipePtr;
        setTimeout(stopAliceNative, 15);
      }
      return;
    }

    var override = getOverrideUrl();
    if (!override) {
      state.lastConsumedOverride = '';
    }
    var normGain = extractNormGain(override);
    var normTag = normGain !== null ? ' (norm=' + normGain.toFixed(4) + ')' : '';
    var isSoup = str.indexOf('yio_souphttpsrc') !== -1;
    var isFileMusic = !isSoup && str.indexOf('filesrc') !== -1 && str.indexOf('/sounds/') === -1;
    if (override && (isSoup || isFileMusic)) {
      if (state.lastConsumedOverride === override) {
        var mutedPrefetch = mutePipeline(str);
        writeLog('[GST_PARSE_LAUNCH] Muted Yandex prefetch/auto-advance while external active\n');
        this.pipePtr = Memory.allocUtf8String(mutedPrefetch);
        args[0] = this.pipePtr;
        return;
      }
      state.lastConsumedOverride = override;
      if (isSoup) {
        var modified = str;
        modified = modified.replace(/alt-locations="[^"]*"\s*/g, '');
        modified = modified.replace(/location="[^"]*"/g, 'location="' + override + '"');
        modified = modified.replace(/!\s*aesctrdec\s+key=[0-9a-fA-F]+\s*!/g, '!');
        modified = applyNormGain(modified, normGain);
        writeLog('[GST_PARSE_LAUNCH] External URL -> ' + override.substr(0, 120) + normTag + '\n');
        this.pipePtr = Memory.allocUtf8String(modified);
        args[0] = this.pipePtr;
      } else {
        var modFile = str.replace(
          /filesrc\s+location="[^"]*"(?:\s*!\s*aesctrdec\s+key=[0-9a-fA-F]+)?\s*!/g,
          'souphttpsrc location="' + override + '" name=yio_souphttpsrc !'
        );
        modFile = applyNormGain(modFile, normGain);
        writeLog('[GST_PARSE_LAUNCH] External filesrc -> ' + override.substr(0, 120) + normTag + '\n');
        this.pipePtr = Memory.allocUtf8String(modFile);
        args[0] = this.pipePtr;
      }
    } else if ((isSoup || isFileMusic) && now < state.musicMuteUntil) {
      var mutedMusic = mutePipeline(str);
      writeLog('[GST_PARSE_LAUNCH] Muted unwanted native music stream\n');
      this.pipePtr = Memory.allocUtf8String(mutedMusic);
      args[0] = this.pipePtr;
    }
  } catch (e) {
    writeLog('[GST_PARSE_LAUNCH] Error: ' + e + '\n');
  }
}

function makeOnSeek(realSeekSimple) {
  return function(elem, rate, format, flags, startType, startPos, stopType, stopPos) {
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
  };
}

function attachHooks() {
  var state = getHookState();
  var parseLaunchAddr = Module.findExportByName(LIB_GST, 'gst_parse_launch');
  var seekAddr = Module.findExportByName(LIB_GST, 'gst_element_seek');
  var seekSimpleAddr = Module.findExportByName(LIB_GST, 'gst_element_seek_simple');

  if (!parseLaunchAddr) {
    return false;
  }

  state.onParseLaunch = handleParseLaunch;
  if (seekAddr && seekSimpleAddr) {
    var realSeekSimple = new NativeFunction(seekSimpleAddr, 'int', ['pointer', 'int', 'int', 'int64']);
    state.onSeek = makeOnSeek(realSeekSimple);
  }

  if (state.isAttached) {
    writeLog('NATIVE INJECTION HOOKS RELOADED\n');
    return true;
  }
  state.isAttached = true;

  // 1. Hook gst_parse_launch via mutable delegate so re-injection hot-swaps logic
  Interceptor.attach(parseLaunchAddr, {
    onEnter: function(args) {
      var fn = getHookState().onParseLaunch;
      if (fn) {
        fn.call(this, args);
      }
    }
  });

  // 2. Hook gst_element_seek via mutable delegate
  if (seekAddr && seekSimpleAddr) {
    Interceptor.replace(seekAddr, new NativeCallback(function(elem, rate, format, flags, startType, startPos, stopType, stopPos) {
      var fn = getHookState().onSeek;
      if (fn) {
        return fn(elem, rate, format, flags, startType, startPos, stopType, stopPos);
      }
      return 1;
    }, 'int', ['pointer', 'double', 'int', 'int', 'int', 'int64', 'int', 'int64']));
    writeLog('[GST_SEEK] Hooked gst_element_seek via realSeekSimple\n');
  }

  writeLog('NATIVE INJECTION HOOKS ATTACHED\n');
  return true;
}

(function startRuntime() {
  var state = getHookState();
  if (state.cancelTimer) {
    clearInterval(state.cancelTimer);
    state.cancelTimer = null;
  }
  if (state.attachTimer) {
    clearInterval(state.attachTimer);
    state.attachTimer = null;
  }
  readCancelState();
  state.cancelTimer = setInterval(readCancelState, 40);
  if (!attachHooks()) {
    state.attachTimer = setInterval(function() {
      if (attachHooks()) {
        clearInterval(state.attachTimer);
        state.attachTimer = null;
      }
    }, 500);
  }
})();
