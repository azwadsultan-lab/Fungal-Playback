/*
  fungal_playback.ino
  Waveform playback prototype for an ESP32-S3-DevKitC-1.

  Plays a stored waveform (a test sine, or a segment of a published fungal
  electrical recording held in waveform.h) as an analogue voltage, with
  amplitude and playback time adjustable over the serial port.

  The ESP32-S3 has no DAC, so the voltage is made with PWM and a low-pass
  RC filter, and read back on an ADC pin so it can be plotted without an
  oscilloscope.

  Wiring
    GPIO2 (PWM) ---[ 1 kOhm ]---+--- GPIO1 (ADC1)
                                |
                              [22 uF]   (electrolytic: + leg to the node)
                                |
                               GND

    Filter cut-off = 1 / (2*pi*R*C) = about 7 Hz.
    GPIO2, GPIO1 and a G pin are all on the same header (G, TX, RX, 1, 2, ...).

  LIMIT: the output is 0 to 3.3 V. Published fungal signals are around
  0.03 to 2.1 mV, more than a thousand times smaller. This sketch shows
  playback and control only. Do not connect it to anything living.

  Serial commands (115200 baud, newline at the end of each line)
    a <0-100>   amplitude, percent of full scale
    t <seconds> time for one pass through the waveform (0.5 to 3600)
    s           sine test waveform
    w           recorded waveform from waveform.h
    q           stream on/off (turn off to read the status text)
    ?           status and help

  Works with Arduino-ESP32 core 2.x and 3.x.
*/

#include <Arduino.h>
#include <math.h>
#include "waveform.h"

// ---------------- Pins ----------------
const int PWM_PIN = 2;  // any free output-capable GPIO
const int ADC_PIN = 1;  // must be an ADC1 pin: GPIO1 to GPIO10 on the S3

// ---------------- PWM -----------------
const uint32_t PWM_FREQ_HZ = 20000;  // far above the 7 Hz filter cut-off
const uint8_t PWM_RES_BITS = 10;     // duty 0 to 1023
const uint32_t PWM_MAX = (1UL << PWM_RES_BITS) - 1;
const uint8_t PWM_CHANNEL = 0;       // used by core 2.x only
const float SUPPLY_MV = 3300.0f;

// ---------------- Timing --------------
const uint32_t UPDATE_PERIOD_US = 1000;  // refresh the output 1000 times a second
const uint32_t REPORT_PERIOD_MS = 20;    // 50 plot lines a second

const float PERIOD_MIN_S = 0.5f;
const float PERIOD_MAX_S = 3600.0f;

// ---------------- State ---------------
enum Mode { MODE_SINE, MODE_WAVE };

Mode mode = WAVE_IS_PLACEHOLDER ? MODE_SINE : MODE_WAVE;
float amplitude = 0.80f;  // 0 to 1. 0.8 keeps the swing inside the ADC range.
float periodS = 10.0f;    // seconds for one pass
double phase = 0.0;       // position in the waveform, 0 to 1
float outFraction = 0.5f; // last value sent to the pin, 0 to 1
bool streaming = true;

uint32_t lastUpdateUs = 0;
uint32_t lastReportMs = 0;

char lineBuf[32];
uint8_t lineLen = 0;

// ---------------- PWM helpers ---------
void pwmBegin() {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcAttach(PWM_PIN, PWM_FREQ_HZ, PWM_RES_BITS);
#else
  ledcSetup(PWM_CHANNEL, PWM_FREQ_HZ, PWM_RES_BITS);
  ledcAttachPin(PWM_PIN, PWM_CHANNEL);
#endif
}

void pwmWrite(uint32_t duty) {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcWrite(PWM_PIN, duty);
#else
  ledcWrite(PWM_CHANNEL, duty);
#endif
}

// ---------------- Waveform ------------
// Returns the waveform value, 0 to 1, at a position p from 0 to 1.
float sampleAt(double p) {
  if (mode == MODE_SINE) {
    return 0.5f + 0.5f * sinf((float)(2.0 * M_PI * p));
  }
  // Recorded waveform: straight-line interpolation between stored points.
  double pos = p * WAVE_LEN;
  uint32_t i0 = (uint32_t)pos;
  if (i0 >= WAVE_LEN) i0 = WAVE_LEN - 1;
  uint32_t i1 = (i0 + 1) % WAVE_LEN;
  float frac = (float)(pos - (double)i0);
  float v = (1.0f - frac) * (float)WAVE[i0] + frac * (float)WAVE[i1];
  return v / (float)WAVE_FULL_SCALE;
}

void updateOutput(uint32_t nowUs) {
  uint32_t dtUs = nowUs - lastUpdateUs;  // correct across micros() overflow
  lastUpdateUs = nowUs;

  phase += (double)dtUs / ((double)periodS * 1e6);
  phase -= floor(phase);  // keep in 0 to 1

  // Scale about mid-rail so a smaller amplitude stays centred on 1.65 V.
  outFraction = 0.5f + (sampleAt(phase) - 0.5f) * amplitude;
  if (outFraction < 0.0f) outFraction = 0.0f;
  if (outFraction > 1.0f) outFraction = 1.0f;

  pwmWrite((uint32_t)lroundf(outFraction * (float)PWM_MAX));
}

// ---------------- Serial --------------
void printStatus() {
  Serial.println("# ---- fungal_playback ----");
  Serial.printf("# mode      : %s\n", mode == MODE_SINE ? "sine test" : "recorded waveform");
  Serial.printf("# amplitude : %.0f %%\n", amplitude * 100.0f);
  Serial.printf("# pass time : %.2f s\n", periodS);
  if (mode == MODE_WAVE) {
    Serial.printf("# source    : %s\n", WAVE_SOURCE);
    Serial.printf("# segment   : %.0f s of recording, %u points, %.3f to %.3f %s\n",
                  WAVE_SRC_SECONDS, (unsigned)WAVE_LEN, WAVE_SRC_MIN, WAVE_SRC_MAX, WAVE_SRC_UNITS);
    Serial.printf("# speed-up  : %.0f times faster than recorded\n", WAVE_SRC_SECONDS / periodS);
    if (WAVE_IS_PLACEHOLDER) {
      Serial.println("# WARNING   : waveform.h is a synthetic placeholder, not fungal data.");
    }
  }
  Serial.println("# commands  : a <0-100> | t <seconds> | s | w | q | ?");
}

void handleLine(char *line) {
  char cmd = line[0];
  float value = atof(line + 1);

  switch (cmd) {
    case 'a':
      if (value < 0.0f) value = 0.0f;
      if (value > 100.0f) value = 100.0f;
      amplitude = value / 100.0f;
      Serial.printf("# amplitude set to %.0f %%\n", value);
      break;
    case 't':
      if (value < PERIOD_MIN_S) value = PERIOD_MIN_S;
      if (value > PERIOD_MAX_S) value = PERIOD_MAX_S;
      periodS = value;
      Serial.printf("# pass time set to %.2f s\n", periodS);
      break;
    case 's':
      mode = MODE_SINE;
      phase = 0.0;
      Serial.println("# mode: sine test");
      break;
    case 'w':
      mode = MODE_WAVE;
      phase = 0.0;
      Serial.println("# mode: recorded waveform");
      if (WAVE_IS_PLACEHOLDER) {
        Serial.println("# WARNING: waveform.h is a synthetic placeholder, not fungal data.");
      }
      break;
    case 'q':
      streaming = !streaming;
      Serial.println(streaming ? "# stream on" : "# stream off");
      break;
    case '?':
      printStatus();
      break;
    default:
      Serial.println("# unknown command, send ? for help");
      break;
  }
}

void readSerial() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (lineLen > 0) {
        lineBuf[lineLen] = '\0';
        handleLine(lineBuf);
        lineLen = 0;
      }
    } else if (lineLen < sizeof(lineBuf) - 1) {
      lineBuf[lineLen++] = c;
    }
  }
}

// ---------------- Arduino entry points
void setup() {
  Serial.begin(115200);
  delay(500);

  pwmBegin();
  pwmWrite(PWM_MAX / 2);  // start at mid-rail

  // The ADC is left at the core's default attenuation, which reads up to
  // roughly 3.1 V on the ESP32-S3.

  lastUpdateUs = micros();
  lastReportMs = millis();
  printStatus();
}

void loop() {
  readSerial();

  uint32_t nowUs = micros();
  if ((uint32_t)(nowUs - lastUpdateUs) >= UPDATE_PERIOD_US) {
    updateOutput(nowUs);
  }

  uint32_t nowMs = millis();
  if (streaming && (uint32_t)(nowMs - lastReportMs) >= REPORT_PERIOD_MS) {
    lastReportMs = nowMs;
    uint32_t readMv = analogReadMilliVolts(ADC_PIN);
    // "label:value" pairs are picked up by the Arduino IDE Serial Plotter.
    Serial.printf("set_mV:%.0f,read_mV:%u\n", outFraction * SUPPLY_MV, (unsigned)readMv);
  }
}
