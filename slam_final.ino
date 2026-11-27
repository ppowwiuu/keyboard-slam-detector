/*
  SLAM GUARD  -  keyboard slam detector (final version)
  Pressure sensor (FSR406) + vibration module + 2 buttons + buzzer + 2 LEDs
  Board : Arduino Uno R3        Serial : 115200        No soldering, no library to install

  ---------------- WIRING ----------------
    FSR406 (pressure)   one leg -> 5V, other leg -> A1, and a 10k resistor between A1 and GND
    Vibration module    S -> A0,  + -> 5V,  - -> GND          (no resistor needed)
    Button L (left)     one leg -> A2, other leg -> GND       (no resistor needed)
    Button R (right)    one leg -> A3, other leg -> GND
    Passive buzzer      + -> D8,  - -> GND
    Green LED           D9  -> 220 ohm -> long leg,  short leg -> GND
    Red LED             D10 -> 220 ohm -> long leg,  short leg -> GND

  ---------------- HOW IT DECIDES ----------------
    press = how much the FSR value JUMPED within the last 0.16 s.
            Slow changes (keyboard weight, resting wrists, moving the book) are ignored.
    shake = vibration module value minus its resting value.
    Mode BOTH (default): press >= pressTh AND shake >= shakeTh within 0.15 s = one strong hit.
    hitsNeeded strong hits within 1.5 s -> warning on the screen ("locked").
    The warning goes away only when BOTH buttons are held together for holdMs (default 3 s).

  ---------------- ARDUINO -> PC (one line each) ----------------
    HELLO,SLAMGUARD,3                       program started (hands off for 1 s)
    BASE,fsrRaw,shakeRest,shakeNoise        resting values
    CFG,pressTh,shakeTh,holdMs,hitsNeeded,mode,muted
    READY
    V,press,shake,fsrRaw                    live values, 20 times per second
    HIT,press,shake,strong                  one tap or slam finished (strong 1 = it counted)
    STRONG,n,need                           strong hit n of need (only when need > 1)
    SLAM,press,shake,total,test             WARNING! (test 1 = from the test button)
    BTN,left,right                          button state changed (1 = pressed)
    HOLD,ms                                 both buttons held for ms (0 = let go)
    CLEAR                                   warning cleared
    CAL,secondsLeft                         typing calibration running
    CALDONE,pressTh,shakeTh,typingPress,typingShakeMax,taps

  ---------------- PC -> ARDUINO (end with newline) ----------------
    W80    pressure threshold          P200   vibration threshold
    D3     mode 0 = pressure only, 1 = vibration only, 2 = either one, 3 = both (recommended)
    N1     strong hits needed (1..3)   T3000  hold time in ms
    M0/M1  buzzer on / off             K      10 s typing calibration
    B      measure resting values again (hands off)
    X      test warning                C      clear warning (screen button)      ?  settings
*/

#include <EEPROM.h>

// ---------------- pins ----------------
const int PIN_SHAKE = A0;
const int PIN_FSR   = A1;
const int PIN_BTN_L = A2;
const int PIN_BTN_R = A3;
const int PIN_BUZZ  = 8;
const int PIN_GREEN = 9;
const int PIN_RED   = 10;
const bool NEED_BOTH_BUTTONS = true;   // false = one button is enough

// ---------------- settings (saved in EEPROM, changeable from the screen) ----------------
enum { MODE_PRESS = 0, MODE_SHAKE = 1, MODE_EITHER = 2, MODE_BOTH = 3 };
struct Config {
  uint16_t     magic;
  int          pressTh;      // pressure jump needed
  int          shakeTh;      // vibration needed
  unsigned int holdMs;       // how long both buttons must be held
  byte         hitsNeeded;   // strong hits within 1.5 s
  byte         mode;
  byte         muted;
};
const uint16_t MAGIC = 0x5A33;   // new number = old saved settings are ignored
Config cfg;

// ---------------- timing ----------------
const unsigned long FSR_EVERY_MS = 4;
const byte          FSR_HIST     = 40;     // 40 x 4 ms = 0.16 s window for "press"
const unsigned long PAIR_MS      = 150;    // both sensors within this time
const unsigned long REFRACT_MS   = 400;    // one slam is counted only once
const unsigned long COMBO_MS     = 1500;
const unsigned long REPORT_MS    = 50;
const unsigned long CAL_MS       = 10000;
const unsigned long EV_QUIET_MS  = 80;
const unsigned long EV_MAX_MS    = 800;

// ---------------- state ----------------
int   fsrHist[FSR_HIST];
byte  fsrIdx = 0;
int   fsrRaw = 0, press = 0, shake = 0;
float shakeRest = 0;
int   shakeNoise = 0;
int   evPressMin = 12, evShakeMin = 15;
unsigned long lastFsrT = 0;

unsigned long pressHiT = 0, shakeHiT = 0, lastCountT = 0, firstStrongT = 0;
byte  strongCount = 0;

bool  evOn = false, evStrong = false;
int   evPress = 0, evShake = 0;
unsigned long evStartT = 0, evQuietT = 0;

int   repPress = 0, repShake = 0;
unsigned long lastRepT = 0;

bool  locked = false;
unsigned long slamTotal = 0, holdStartT = 0, lastHoldRepT = 0;
byte  btnStable = 0, btnLastRaw = 0;
int   btnSent = -1;
unsigned long lastBtnT = 0;

bool  calibrating = false;
unsigned long calEndT = 0, lastCalRepT = 0;
int   calPressMax = 0, calShakeMax = 0;
const byte CAL_N = 64;
int   calPress[CAL_N];                  // pressure of each typing tap during calibration
byte  calCount = 0;

const int ALARM_TONES[4] = {988, 784, 988, 784};
byte  alarmIdx = 255;
unsigned long alarmNextT = 0;

char  lineBuf[16];
byte  lineLen = 0;

// ============================================================
//  settings
// ============================================================
void loadConfig() {
  EEPROM.get(0, cfg);
  if (cfg.magic != MAGIC) {
    cfg.magic      = MAGIC;
    cfg.pressTh    = 80;
    cfg.shakeTh    = 200;
    cfg.holdMs     = 3000;
    cfg.hitsNeeded = 1;
    cfg.mode       = MODE_BOTH;
    cfg.muted      = 0;
    EEPROM.put(0, cfg);
  }
}
void saveConfig() { EEPROM.put(0, cfg); }

void printConfig() {
  Serial.print(F("CFG,"));
  Serial.print(cfg.pressTh);    Serial.print(',');
  Serial.print(cfg.shakeTh);    Serial.print(',');
  Serial.print(cfg.holdMs);     Serial.print(',');
  Serial.print(cfg.hitsNeeded); Serial.print(',');
  Serial.print(cfg.mode);       Serial.print(',');
  Serial.println(cfg.muted);
}
void printBase() {
  Serial.print(F("BASE,"));
  Serial.print(fsrRaw);         Serial.print(',');
  Serial.print((int)shakeRest); Serial.print(',');
  Serial.println(shakeNoise);
}

// ============================================================
//  resting values (hands off!)
// ============================================================
int readShakeRaw() {
  analogRead(PIN_SHAKE);              // first reading after switching pins is thrown away
  return analogRead(PIN_SHAKE);
}

void measureRest() {
  long fsum = 0, ssum = 0;
  int  smax = 0;
  for (int i = 0; i < 200; i++) {
    fsum += analogRead(PIN_FSR);
    int s = readShakeRaw();
    ssum += s;
    if (s > smax) smax = s;
    delay(2);
  }
  fsrRaw = (int)(fsum / 200);
  shakeRest = ssum / 200.0;
  shakeNoise = smax - (int)shakeRest;
  if (shakeNoise < 0) shakeNoise = 0;
  for (byte i = 0; i < FSR_HIST; i++) fsrHist[i] = fsrRaw;
  press = 0; shake = 0;
  evShakeMin = shakeNoise * 2 + 5;
  if (evShakeMin < 15) evShakeMin = 15;
}

// ============================================================
//  buzzer (does not stop the program)
// ============================================================
void startAlarm() {
  if (cfg.muted) return;
  alarmIdx = 0;
  alarmNextT = millis();
}
void updateAlarm(unsigned long now) {
  if (alarmIdx >= 4) return;
  if ((long)(now - alarmNextT) >= 0) {
    tone(PIN_BUZZ, ALARM_TONES[alarmIdx], 140);
    alarmNextT = now + 190;
    alarmIdx++;
  }
}
void beep(int freq, int ms) {
  if (cfg.muted) return;
  alarmIdx = 255;
  tone(PIN_BUZZ, freq, ms);
}

// ============================================================
//  warning on / off
// ============================================================
void triggerSlam(int p, int s, bool test) {
  slamTotal++;
  locked = true;
  holdStartT = 0;
  strongCount = 0;
  Serial.print(F("SLAM,"));
  Serial.print(p);         Serial.print(',');
  Serial.print(s);         Serial.print(',');
  Serial.print(slamTotal); Serial.print(',');
  Serial.println(test ? 1 : 0);
  startAlarm();
}

void clearWarning() {
  locked = false;
  holdStartT = 0;
  strongCount = 0;
  Serial.println(F("CLEAR"));
  beep(1319, 120);
}

void countStrong(unsigned long now) {
  lastCountT = now;
  evStrong = true;
  if (strongCount == 0 || now - firstStrongT > COMBO_MS) { strongCount = 0; firstStrongT = now; }
  strongCount++;
  if (strongCount >= cfg.hitsNeeded) {
    triggerSlam(evPress, evShake, false);
  } else {
    Serial.print(F("STRONG,")); Serial.print(strongCount);
    Serial.print(',');          Serial.println(cfg.hitsNeeded);
  }
}

// decide if this moment is a strong hit
void checkStrong(unsigned long now) {
  bool pNow = press >= cfg.pressTh;
  bool sNow = shake >= cfg.shakeTh;
  if (pNow) pressHiT = now;
  if (sNow) shakeHiT = now;
  bool hit = false;
  if (cfg.mode == MODE_PRESS)       hit = pNow;
  else if (cfg.mode == MODE_SHAKE)  hit = sNow;
  else if (cfg.mode == MODE_EITHER) hit = pNow || sNow;
  else {
    bool pRecent = pressHiT != 0 && now - pressHiT <= PAIR_MS;
    bool sRecent = shakeHiT != 0 && now - shakeHiT <= PAIR_MS;
    hit = (pNow || sNow) && pRecent && sRecent;
  }
  if (hit && (lastCountT == 0 || now - lastCountT >= REFRACT_MS)) countStrong(now);
}

// one tap / slam from start to end, for the log on the screen
void updateEvent(unsigned long now) {
  bool active = press >= evPressMin || shake >= evShakeMin;
  if (!evOn) {
    if (active) {
      evOn = true; evStrong = false;
      evPress = press; evShake = shake;
      evStartT = now; evQuietT = 0;
    }
    return;
  }
  if (press > evPress) evPress = press;
  if (shake > evShake) evShake = shake;
  if (active) evQuietT = 0;
  else if (evQuietT == 0) evQuietT = now;
  if ((evQuietT != 0 && now - evQuietT >= EV_QUIET_MS) || now - evStartT >= EV_MAX_MS) {
    evOn = false;
    if (calibrating && calCount < CAL_N) calPress[calCount++] = evPress;
    Serial.print(F("HIT,"));
    Serial.print(evPress); Serial.print(',');
    Serial.print(evShake); Serial.print(',');
    Serial.println(evStrong ? 1 : 0);
  }
}

// ============================================================
//  typing calibration: 10 s of normal typing -> thresholds
// ============================================================
void startCal() {
  calibrating = true;
  calEndT = millis() + CAL_MS;
  lastCalRepT = 0;
  calPressMax = 0; calShakeMax = 0; calCount = 0;
  strongCount = 0;
}

// pressure of the typing taps, ignoring the top 10% (a wrist landing once is not typing)
int typingPress90() {
  if (calCount < 5) return calPressMax;
  for (byte i = 1; i < calCount; i++) {          // small sort
    int v = calPress[i]; int j = i - 1;
    while (j >= 0 && calPress[j] > v) { calPress[j + 1] = calPress[j]; j--; }
    calPress[j + 1] = v;
  }
  return calPress[(calCount - 1) * 9 / 10];
}
void updateCal(unsigned long now) {
  if (press > calPressMax) calPressMax = press;
  if (shake > calShakeMax) calShakeMax = shake;
  if (lastCalRepT == 0 || now - lastCalRepT >= 1000) {
    lastCalRepT = now;
    long left = (long)(calEndT - now);
    Serial.print(F("CAL,"));
    Serial.println(left > 0 ? (left + 999) / 1000 : 0);
  }
  if ((long)(now - calEndT) >= 0) {
    calibrating = false;
    int p90 = typingPress90();
    long pt = (long)p90 * 2;
    if (pt < p90 + 30L) pt = p90 + 30L;
    if (pt < 25)  pt = 25;
    if (pt > 400) pt = 400;
    long st = (long)calShakeMax * 2;
    if (st < calShakeMax + 60L) st = calShakeMax + 60L;
    if (st < 40)  st = 40;
    if (st > 900) st = 900;
    cfg.pressTh = (int)pt;
    cfg.shakeTh = (int)st;
    saveConfig();
    Serial.print(F("CALDONE,"));
    Serial.print(cfg.pressTh); Serial.print(',');
    Serial.print(cfg.shakeTh); Serial.print(',');
    Serial.print(p90);         Serial.print(',');
    Serial.print(calShakeMax); Serial.print(',');
    Serial.println(calCount);
    printConfig();
    lastCountT = now;          // ignore the last key of the calibration
    beep(1047, 80);
  }
}

// ============================================================
//  commands from the screen
// ============================================================
void handleLine(char *s) {
  char c = s[0];
  if (c >= 'a' && c <= 'z') c -= 32;
  bool hasNum = (s[1] >= '0' && s[1] <= '9');
  long n = atol(s + 1);
  switch (c) {
    case 'W': if (hasNum && n >= 5 && n <= 1000) { cfg.pressTh = n; saveConfig(); } break;
    case 'P': if (hasNum && n >= 5 && n <= 1023) { cfg.shakeTh = n; saveConfig(); } break;
    case 'D': if (hasNum && n <= 3) { cfg.mode = n; saveConfig(); strongCount = 0; } break;
    case 'N': if (hasNum && n >= 1 && n <= 3) { cfg.hitsNeeded = n; saveConfig(); strongCount = 0; } break;
    case 'T': if (hasNum && n >= 500 && n <= 20000) { cfg.holdMs = n; saveConfig(); } break;
    case 'M':
      cfg.muted = hasNum ? (n ? 1 : 0) : !cfg.muted;
      saveConfig();
      if (cfg.muted) { alarmIdx = 255; noTone(PIN_BUZZ); }
      break;
    case 'K': startCal(); return;
    case 'B': measureRest(); printBase(); return;
    case 'X': triggerSlam(0, 0, true); return;
    case 'C': if (locked) clearWarning(); return;
    case '?': printBase(); break;
    default: return;
  }
  printConfig();
}

void readSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (lineLen > 0) { lineBuf[lineLen] = 0; handleLine(lineBuf); lineLen = 0; }
    } else if (lineLen < sizeof(lineBuf) - 1) {
      lineBuf[lineLen++] = c;
    }
  }
}

// ============================================================
//  buttons: both held for holdMs = warning off
// ============================================================
void updateButtons(unsigned long now) {
  if (now - lastBtnT >= 10) {
    lastBtnT = now;
    byte raw = (digitalRead(PIN_BTN_L) == LOW ? 1 : 0) | (digitalRead(PIN_BTN_R) == LOW ? 2 : 0);
    if (raw == btnLastRaw) btnStable = raw;      // same twice in a row = not bouncing
    btnLastRaw = raw;
    if (btnStable != btnSent) {
      btnSent = btnStable;
      Serial.print(F("BTN,"));
      Serial.print(btnStable & 1 ? 1 : 0); Serial.print(',');
      Serial.println(btnStable & 2 ? 1 : 0);
    }
  }
  if (!locked) { holdStartT = 0; return; }
  bool l = btnStable & 1, r = btnStable & 2;
  bool held = NEED_BOTH_BUTTONS ? (l && r) : (l || r);
  if (held) {
    if (holdStartT == 0) { holdStartT = now; lastHoldRepT = now; }
    unsigned long h = now - holdStartT;
    if (h >= cfg.holdMs) {
      clearWarning();
    } else if (now - lastHoldRepT >= 100) {
      lastHoldRepT = now;
      Serial.print(F("HOLD,"));
      Serial.println(h);
    }
  } else if (holdStartT != 0) {
    holdStartT = 0;
    Serial.println(F("HOLD,0"));
  }
}

// ============================================================
//  start
// ============================================================
void setup() {
  pinMode(PIN_BUZZ, OUTPUT);
  pinMode(PIN_GREEN, OUTPUT);
  pinMode(PIN_RED, OUTPUT);
  pinMode(PIN_BTN_L, INPUT_PULLUP);
  pinMode(PIN_BTN_R, INPUT_PULLUP);
  Serial.begin(115200);
  loadConfig();
  Serial.println(F("HELLO,SLAMGUARD,3"));
  delay(300);
  measureRest();
  printBase();
  printConfig();
  Serial.println(F("READY"));
}

// ============================================================
//  loop
// ============================================================
void loop() {
  unsigned long now = millis();
  readSerial();

  // vibration: every loop (fast)
  int s = readShakeRaw() - (int)shakeRest;
  shake = s < 0 ? 0 : s;

  // pressure: every 4 ms. press = now - lowest value of the last 0.16 s
  if (now - lastFsrT >= FSR_EVERY_MS) {
    lastFsrT = now;
    fsrRaw = analogRead(PIN_FSR);
    fsrHist[fsrIdx] = fsrRaw;
    fsrIdx = (fsrIdx + 1) % FSR_HIST;
    int mn = 1023;
    for (byte i = 0; i < FSR_HIST; i++) if (fsrHist[i] < mn) mn = fsrHist[i];
    press = fsrRaw - mn;
  }
  if (press > repPress) repPress = press;
  if (shake > repShake) repShake = shake;

  updateEvent(now);
  if (calibrating) updateCal(now);
  else checkStrong(now);
  if (strongCount > 0 && now - firstStrongT > COMBO_MS) strongCount = 0;

  if (now - lastRepT >= REPORT_MS) {
    lastRepT = now;
    Serial.print(F("V,"));
    Serial.print(repPress); Serial.print(',');
    Serial.print(repShake); Serial.print(',');
    Serial.println(fsrRaw);
    repPress = press; repShake = shake;
  }

  updateButtons(now);
  updateAlarm(now);

  if (locked) {
    digitalWrite(PIN_RED, (now / 150) % 2 ? HIGH : LOW);
    digitalWrite(PIN_GREEN, LOW);
  } else if (calibrating) {
    digitalWrite(PIN_RED, LOW);
    digitalWrite(PIN_GREEN, (now / 250) % 2 ? HIGH : LOW);
  } else {
    digitalWrite(PIN_RED, LOW);
    digitalWrite(PIN_GREEN, HIGH);
  }
}
