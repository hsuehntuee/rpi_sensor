#pragma once

// ---- 腳位（標準接線，見 README「冷氣紅外線遙控」）---------------------------
// IR 發射模組的訊號腳。不能用 GPIO34–39（只能輸入），避開 strapping pin（0/2/15）。
#ifndef IR_LED_PIN
#define IR_LED_PIN 32
#endif
// IR 接收頭（VS1838B / TSOP38238）的 OUT 腳。
#ifndef IR_RECV_PIN
#define IR_RECV_PIN 14
#endif
// 接收頭的 VCC 接在這支 GPIO，韌體輸出 HIGH 供電（接收頭只吃 1–5 mA）。
// -1 = 不使用（接收頭 VCC 改接 3V3）。
#ifndef IR_RECV_PWR_PIN
#define IR_RECV_PWR_PIN 12
#endif

// 這塊板子控制的冷氣（寫死在韌體裡，由 platformio.ini 的 esp32_304D / esp32_614 決定）。
// 樹莓派的 .env 若有填 AC_PROTOCOL 會以樹莓派為準；沒填就用這裡的值。
#ifndef DEFAULT_PROTOCOL
#define DEFAULT_PROTOCOL "HITACHI_AC1"
#endif
#ifndef DEFAULT_MODEL
#define DEFAULT_MODEL ""  // 同一協定有多種遙控器時才需要，例如 "R-LT0541-HTA-B"
#endif

// ---- IR 接收參數 -----------------------------------------------------------
// 冷氣訊號很長（Hitachi 344 bit ≈ 700 個 mark/space），buffer 要夠大。
const uint16_t kCaptureBufferSize = 2048;
// 訊息間隔 >timeout 才視為結束。50ms 可把多段式冷氣訊號收成一筆。
const uint8_t kCaptureTimeoutMs = 50;
// 小於這個長度的 UNKNOWN 訊號視為雜訊。
const uint16_t kMinUnknownSize = 12;
