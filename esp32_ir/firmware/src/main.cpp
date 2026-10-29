// ESP32 冷氣 IR 遙控器
//
// 樹莓派用 USB 線同時供電＋傳指令（序列埠 115200，一行一個 JSON）：
//   {"cmd":"ac","protocol":"HITACHI_AC344","power":true,"mode":"cool","temp":26,"fan":"auto","swingv":"off"}
//       用協定編碼送出完整冷氣狀態；欄位可省略，省略的沿用上一次的值。
//       protocol 省略時用韌體寫死的 DEFAULT_PROTOCOL（esp32_304D=日立、esp32_614=東元）。
//   {"cmd":"status"}                  目前狀態、最後一次收到的 IR
//   {"cmd":"protocols"}               列出可用 "ac" 指令的協定
//   診斷用：raw / selftest / scan / edges / probe（見 handleCommand）
//
// 收到任何 IR（例如按原廠遙控器）都會在序列埠印出 {"event":"ir",...}，
// 用來辨識冷氣是哪個協定；接收頭聽到原廠遙控器的冷氣指令時也會同步內部狀態。

#include <Arduino.h>
#include <ArduinoJson.h>
#include <IRac.h>
#include <IRrecv.h>
#include <IRremoteESP8266.h>
#include <IRsend.h>
#include <IRutils.h>
#include <Preferences.h>

#include "config.h"

IRsend irsend(IR_LED_PIN);
IRac irac(IR_LED_PIN);
IRrecv irrecv(IR_RECV_PIN, kCaptureBufferSize, kCaptureTimeoutMs, true);
decode_results results;
Preferences prefs;

// IR 是單向的：這是「最後一次送出或從原廠遙控器聽到」的狀態，不保證是冷氣實際狀態。
stdAc::state_t acState;
bool haveAcState = false;

JsonDocument lastIr;

// ---------------------------------------------------------------------------
// 冷氣狀態 <-> JSON

void stateToJson(const stdAc::state_t &s, JsonObject o) {
  o["protocol"] = typeToString(s.protocol);
  o["model"] = s.model;
  o["power"] = s.power;
  o["mode"] = IRac::opmodeToString(s.mode);
  o["temp"] = s.degrees;
  o["fan"] = IRac::fanspeedToString(s.fanspeed);
  o["swingv"] = IRac::swingvToString(s.swingv);
}

// 數值與布林也接受字串形式（例如 "26"、"on"）。
static String asStr(JsonVariantConst v) {
  if (v.is<const char *>()) return v.as<const char *>();
  if (v.is<bool>()) return v.as<bool>() ? "on" : "off";
  if (v.is<float>()) return String(v.as<float>());
  return "";
}

void applyJsonToState(JsonVariantConst in, stdAc::state_t &s) {
  if (!in["protocol"].isNull()) {
    decode_type_t p = strToDecodeType(asStr(in["protocol"]).c_str());
    if (p != s.protocol) s.model = -1;  // 換協定時，舊協定的型號沒有意義
    s.protocol = p;
  }
  if (!in["model"].isNull()) {
    if (in["model"].is<int>()) s.model = in["model"].as<int>();
    else s.model = IRac::strToModel(asStr(in["model"]).c_str(), s.model);
  }
  if (!in["power"].isNull())
    s.power = IRac::strToBool(asStr(in["power"]).c_str(), s.power);
  if (!in["mode"].isNull()) {
    stdAc::opmode_t m = IRac::strToOpmode(asStr(in["mode"]).c_str(), s.mode);
    if (m == stdAc::opmode_t::kOff) s.power = false;  // 保留原模式，只關機
    else s.mode = m;
  }
  if (!in["temp"].isNull()) s.degrees = asStr(in["temp"]).toFloat();
  if (!in["fan"].isNull())
    s.fanspeed = IRac::strToFanspeed(asStr(in["fan"]).c_str(), s.fanspeed);
  if (!in["swingv"].isNull())
    s.swingv = IRac::strToSwingV(asStr(in["swingv"]).c_str(), s.swingv);
}

void saveAcState() {
  JsonDocument d;
  stateToJson(acState, d.to<JsonObject>());
  String out;
  serializeJson(d, out);
  prefs.putString("ac", out);
}

void loadAcState() {
  IRac::initState(&acState);
  acState.protocol = strToDecodeType(DEFAULT_PROTOCOL);
  acState.model = IRac::strToModel(DEFAULT_MODEL, -1);
  acState.mode = stdAc::opmode_t::kCool;
  acState.degrees = 26;
  String saved = prefs.getString("ac", "");
  JsonDocument d;
  // 只沿用同一協定的舊狀態；板子改燒成另一台冷氣的韌體時，從預設值重新開始
  if (saved.length() && !deserializeJson(d, saved) &&
      strToDecodeType(d["protocol"] | "") == acState.protocol) {
    applyJsonToState(d.as<JsonVariantConst>(), acState);
    haveAcState = true;
  }
}

// ---------------------------------------------------------------------------
// HITACHI_AC1（304D 日立，R-LT0541-HTA-B 遙控器）
// 實測原廠遙控器：Power 位元永遠是 0，開關機只靠 PowerToggle（按一次就切換），沒有擺風；
// 除濕送溫度 16°C＋弱風，送風溫度欄位為 0。函式庫的 IRac 會把 Power 設成 1，
// 所以這裡照遙控器的格式自己組。

bool sendHitachiAc1(const stdAc::state_t &next, const stdAc::state_t *prev) {
  IRHitachiAc1 ac(IR_LED_PIN);
  ac.begin();
  ac.setModel(next.model >= 0 ? (hitachi_ac1_remote_model_t)next.model
                              : hitachi_ac1_remote_model_t::R_LT0541_HTA_B);
  // 函式庫在送風模式下不會套用非自動的風速，所以先在冷氣模式設好風速再切模式
  ac.setMode(kHitachiAc1Cool);
  ac.setFan(ac.convertFan(next.fanspeed));
  ac.setMode(ac.convertMode(next.mode));
  ac.setTemp(next.mode == stdAc::opmode_t::kDry ? 16 : next.degrees);
  ac.setSwingV(false);
  ac.setSwingH(false);
  ac.setSwingToggle(false);
  ac.setPower(false);
  // 不知道冷氣目前狀態時（第一次送），假設這次是要切換電源
  const bool prevPower = prev ? prev->power : !next.power;
  ac.setPowerToggle(next.power != prevPower);
  if (next.mode == stdAc::opmode_t::kFan) ac.getRaw()[6] = 0;  // 送風：遙控器這個 byte 送 0
  ac.send();
  return true;
}

// 原廠遙控器按了電源鍵（PowerToggle）→ 翻轉我們記錄的開關狀態；Power 位元本身不可信
void syncHitachiAc1Power(const decode_results &r, stdAc::state_t &heard) {
  IRHitachiAc1 ac(IR_LED_PIN);
  ac.setRaw(r.state);
  const bool before = haveAcState ? acState.power : false;
  heard.power = ac.getPowerToggle() ? !before : before;
}

// ---------------------------------------------------------------------------
// ELECTRA_AC（614 東元）
// 實測原廠遙控器：byte 11 是「這次按了哪顆鍵」：溫度+ 0x40、溫度- 0x41、左右擺風 0x43、
// 風速 0x44、電源 0x45、模式 0x46。函式庫固定送 0x08（遙控器從不送），這裡改成依變化填鍵碼。

uint8_t electraKeyCode(const stdAc::state_t &next, const stdAc::state_t *prev) {
  if (!prev || prev->power != next.power) return 0x45;
  if (prev->mode != next.mode) return 0x46;
  if (prev->fanspeed != next.fanspeed) return 0x44;
  if (prev->swingh != next.swingh) return 0x43;
  if (next.degrees < prev->degrees) return 0x41;
  return 0x40;  // 溫度+（上下擺風單獨改變時也用這個；沒有實測到上下擺風的鍵碼）
}

bool sendElectra(const stdAc::state_t &next, const stdAc::state_t *prev) {
  IRElectraAc ac(IR_LED_PIN);
  ac.begin();
  ac.setPower(next.power);
  ac.setMode(ac.convertMode(next.mode));
  ac.setTemp(next.degrees);
  ac.setFan(ac.convertFan(next.fanspeed));
  ac.setSwingV(next.swingv != stdAc::swingv_t::kOff);
  ac.setSwingH(next.swingh != stdAc::swingh_t::kOff);
  ac.getRaw()[11] = electraKeyCode(next, prev);
  ac.send();
  return true;
}

// ---------------------------------------------------------------------------
// 指令處理

// 迴路測試：送出時接收器不關（"loopback":true），送完把自己收到的訊號解碼回報。
// IR LED 要對著接收頭。
bool in_raw_debug = false;  // {"raw_debug":true} 時迴路結果附上完整原始時序

void reportLoopback(JsonDocument &out) {
  delay(kCaptureTimeoutMs + 200);
  decode_results r;
  JsonObject lb = out["loopback"].to<JsonObject>();
  lb["received"] = irrecv.decode(&r);
  if (lb["received"]) {
    lb["protocol"] = typeToString(r.decode_type, r.repeat);
    lb["bits"] = r.bits;
    lb["hex"] = resultToHexidecimal(&r);
    String ac = IRAcUtils::resultAcToString(&r);
    if (ac.length()) lb["ac"] = ac;
    if (in_raw_debug) {
      uint16_t len = getCorrectedRawLength(&r);
      uint16_t *arr = resultToRawArray(&r);
      lb["raw_len"] = len;
      JsonArray raw = lb["raw"].to<JsonArray>();
      for (uint16_t i = 0; i < len; i++) raw.add(arr[i]);
      delete[] arr;
    }
  }
  irrecv.resume();
}

void handleCommand(JsonVariantConst in, JsonDocument &out) {
  String cmd = in["cmd"] | "status";
  bool loopback = in["loopback"] | false;
  in_raw_debug = in["raw_debug"] | false;
  out["cmd"] = cmd;
  out["ok"] = true;

  if (cmd == "ac") {
    stdAc::state_t next = acState;
    applyJsonToState(in, next);
    if (!IRac::isProtocolSupported(next.protocol)) {
      out["ok"] = false;
      out["error"] = "unsupported protocol";
      return;
    }
    bool sameProto = haveAcState && acState.protocol == next.protocol;
    if (loopback) irrecv.resume(); else irrecv.disableIRIn();
    const stdAc::state_t *prev = sameProto ? &acState : nullptr;
    bool ok;
    if (next.protocol == decode_type_t::HITACHI_AC1) ok = sendHitachiAc1(next, prev);
    else if (next.protocol == decode_type_t::ELECTRA_AC) ok = sendElectra(next, prev);
    else ok = irac.sendAc(next, prev);
    if (loopback) reportLoopback(out); else irrecv.enableIRIn();
    if (!ok) {
      out["ok"] = false;
      out["error"] = "sendAc failed";
      return;
    }
    acState = next;
    haveAcState = true;
    saveAcState();
    stateToJson(acState, out["ac"].to<JsonObject>());

  } else if (cmd == "protocols") {
    JsonArray arr = out["protocols"].to<JsonArray>();
    for (int i = 0; i <= kLastDecodeType; i++) {
      decode_type_t t = (decode_type_t)i;
      if (IRac::isProtocolSupported(t)) arr.add(typeToString(t));
    }

  } else if (cmd == "raw") {
    // 診斷：{"cmd":"raw","khz":38,"data":[9000,4500,560,...]}  mark/space 交錯，負號會被忽略
    JsonArrayConst data = in["data"];
    uint16_t len = data.size();
    if (!len) {
      out["ok"] = false;
      out["error"] = "data must be a non-empty array";
      return;
    }
    uint16_t *buf = new uint16_t[len];
    for (uint16_t i = 0; i < len; i++) buf[i] = abs(data[i].as<int32_t>());
    if (loopback) irrecv.resume(); else irrecv.disableIRIn();
    irsend.sendRaw(buf, len, in["khz"] | 38);
    delete[] buf;
    if (loopback) reportLoopback(out); else irrecv.enableIRIn();

  } else if (cmd == "edges") {
    // 診斷：暫停 IRrecv，自己數接收腳在送 NEC 期間有幾個電位變化。
    static volatile uint32_t edgeCount;
    edgeCount = 0;
    irrecv.disableIRIn();
    pinMode(IR_RECV_PIN, INPUT);
    attachInterrupt(IR_RECV_PIN, [] { edgeCount++; }, CHANGE);
    irsend.sendNEC(0x20DF10EF);
    delay(20);
    uint32_t duringSend = edgeCount;
    edgeCount = 0;
    delay(in["listen_ms"] | 0);
    detachInterrupt(IR_RECV_PIN);
    out["edges_during_send"] = duringSend;  // NEC 正常約 68 個
    out["edges_while_listening"] = edgeCount;
    out["idle_level"] = digitalRead(IR_RECV_PIN);
    irrecv.enableIRIn();

  } else if (cmd == "scan") {
    // 診斷：送 NEC 時同時監看所有安全的 GPIO（全設 INPUT，高阻抗不會傷硬體），
    // 找出接收頭 OUT 實際接在哪一腳。
    static const uint8_t pins[] = {2, 4, 5, 13, 14, 15, 16, 17, 18, 19, 21, 22, 23,
                                   25, 26, 27, 33, 34, 35, 36, 39};
    static const size_t n = sizeof(pins);
    static volatile uint32_t counts[sizeof(pins)];
    irrecv.disableIRIn();
    for (size_t i = 0; i < n; i++) {
      if (pins[i] == IR_LED_PIN || pins[i] == IR_RECV_PWR_PIN) continue;
      counts[i] = 0;
      pinMode(pins[i], INPUT);
      attachInterruptArg(pins[i], [](void *a) { counts[(size_t)a]++; }, (void *)i, CHANGE);
    }
    delay(5);
    for (size_t i = 0; i < n; i++) counts[i] = 0;  // 丟掉設定時的雜訊
    if (in["send"] | true)
      for (int k = 0; k < 3; k++) { irsend.sendNEC(0x20DF10EF); delay(40); }
    delay(in["listen_ms"] | 0);  // 期間可以拿原廠遙控器對接收頭按
    JsonObject res = out["edges"].to<JsonObject>();
    for (size_t i = 0; i < n; i++) {
      if (pins[i] == IR_LED_PIN || pins[i] == IR_RECV_PWR_PIN) continue;
      detachInterrupt(pins[i]);
      res[String(pins[i])] = counts[i];
    }
    irrecv.enableIRIn();

  } else if (cmd == "probe") {
    // 診斷：接收腳分別開內部下拉／上拉讀值。
    // 有接到接收頭（待機輸出 HIGH）→ 下拉時仍讀 1；腳是空的 → 下拉讀 0、上拉讀 1。
    irrecv.disableIRIn();
    pinMode(IR_RECV_PIN, INPUT_PULLDOWN);
    delay(5);
    out["with_pulldown"] = digitalRead(IR_RECV_PIN);
    pinMode(IR_RECV_PIN, INPUT_PULLUP);
    delay(5);
    out["with_pullup"] = digitalRead(IR_RECV_PIN);
    irrecv.enableIRIn();

  } else if (cmd == "selftest") {
    // 迴路測試：接收器不關，自己送一個 NEC 碼看收不收得到。
    // IR LED 要對著接收頭（幾公分內即可）。
    const uint64_t kTestCode = 0x20DF10EF;
    irrecv.resume();
    irsend.sendNEC(kTestCode);
    delay(150);
    decode_results r;
    bool got = irrecv.decode(&r);
    out["ok"] = got && r.decode_type == decode_type_t::NEC && r.value == kTestCode;
    out["received"] = got ? typeToString(r.decode_type) + " 0x" + uint64ToString(r.value, 16) : "nothing";
    irrecv.resume();

  } else if (cmd == "status") {
    out["firmware_protocol"] = DEFAULT_PROTOCOL;
    out["recv_idle_level"] = digitalRead(IR_RECV_PIN);  // 接收頭待機應為 1
    out["uptime_s"] = millis() / 1000;
    stateToJson(acState, out["ac"].to<JsonObject>());
    out["ac_known"] = haveAcState;
    if (!lastIr.isNull()) out["last_ir"] = lastIr;

  } else {
    out["ok"] = false;
    out["error"] = "unknown cmd";
  }
}

// ---------------------------------------------------------------------------
// IR 接收

void handleIrReceive() {
  if (!irrecv.decode(&results)) return;
  if (results.overflow) Serial.println(F("{\"event\":\"warn\",\"msg\":\"capture buffer overflow\"}"));

  JsonDocument ev;
  ev["event"] = "ir";
  ev["protocol"] = typeToString(results.decode_type, results.repeat);
  ev["bits"] = results.bits;
  ev["raw_len"] = getCorrectedRawLength(&results);
  ev["hex"] = resultToHexidecimal(&results);
  String acDesc = IRAcUtils::resultAcToString(&results);
  if (acDesc.length()) ev["ac"] = acDesc;
  if (results.decode_type == decode_type_t::UNKNOWN) {
    // 不認得的協定附上原始時序，方便在電腦上分析
    uint16_t len = getCorrectedRawLength(&results);
    uint16_t *arr = resultToRawArray(&results);
    JsonArray raw = ev["raw"].to<JsonArray>();
    for (uint16_t i = 0; i < len; i++) raw.add(arr[i]);
    delete[] arr;
  }

  // 聽到原廠遙控器的冷氣指令 → 同步內部狀態
  stdAc::state_t heard;
  if (IRAcUtils::decodeToState(&results, &heard, haveAcState ? &acState : nullptr)) {
    if (results.decode_type == decode_type_t::HITACHI_AC1) syncHitachiAc1Power(results, heard);
    acState = heard;
    haveAcState = true;
    saveAcState();
    ev["synced"] = true;
  }

  lastIr = ev;
  serializeJson(ev, Serial);
  Serial.println();
  irrecv.resume();
}

// ---------------------------------------------------------------------------
// 序列埠

String serialLine;

void handleSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r') continue;
    if (c != '\n') {
      if (serialLine.length() < 4096) serialLine += c;
      continue;
    }
    serialLine.trim();
    if (serialLine.isEmpty()) continue;
    JsonDocument in, out;
    if (deserializeJson(in, serialLine)) {
      out["ok"] = false;
      out["error"] = "bad json";
    } else {
      handleCommand(in.as<JsonVariantConst>(), out);
    }
    serializeJson(out, Serial);
    Serial.println();
    serialLine = "";
  }
}

// ---------------------------------------------------------------------------

void setup() {
  Serial.begin(115200);
  delay(300);
  prefs.begin("esp-ac", false);
  loadAcState();

  if (IR_RECV_PWR_PIN >= 0) {
    pinMode(IR_RECV_PWR_PIN, OUTPUT);
    digitalWrite(IR_RECV_PWR_PIN, HIGH);
    delay(50);  // 等接收頭的 AGC 穩定
  }
  irsend.begin();
  irrecv.setUnknownThreshold(kMinUnknownSize);
  irrecv.setTolerance(kTolerance);
  irrecv.enableIRIn();

  Serial.printf("{\"event\":\"boot\",\"ir_led_pin\":%d,\"ir_recv_pin\":%d}\n", IR_LED_PIN, IR_RECV_PIN);
}

void loop() {
  handleSerial();
  handleIrReceive();
}
