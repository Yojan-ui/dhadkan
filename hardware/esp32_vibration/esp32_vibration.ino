// Dhadkan vibration node: ESP32 + MPU6050 accelerometer -> POST /api/ingest/reading
//
// NOT TESTED ON HARDWARE. Written against the Arduino-ESP32 core 3.x and its bundled WiFi, HTTPClient,
// Wire and base64 (mbedtls) libraries; the server side is tested with tools/sim_device.py only.
//
// Parts (typical listed prices, confirm locally): ESP32 dev board, MPU6050 (GY-521) module, USB power.
// Mount the MPU6050 flat on the motor or bearing housing with a screw or strong magnet, not tape.
//
// What it does every READ_EVERY_S seconds:
//   1. samples the accelerometer's Z axis at ~1 kHz for 2 s (2000 int16 samples)
//   2. sends it with a sequence number; the server ignores a sequence number it has already seen
//   3. when Wi-Fi or the server is down, keeps up to QUEUE_N readings in RAM and resends them oldest first
//      (RAM only: a power cut loses the queue; the sequence number survives in RTC memory across deep sleep)

#include <Arduino.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <Wire.h>
#include "mbedtls/base64.h"

const char* WIFI_SSID = "your-shed-wifi";        // edit
const char* WIFI_PASS = "your-password";         // edit
const char* SERVER    = "http://192.168.1.10:8600/api/ingest/reading";  // the shed PC running make serve
const char* DEVICE    = "esp32-chakki-1";
const char* MACHINE   = "chakki-1";

const int   MPU_ADDR     = 0x68;
const int   SR_HZ        = 1000;
const int   N_SAMPLES    = 2000;
const int   READ_EVERY_S = 600;   // one reading every 10 minutes
const int   QUEUE_N      = 6;

RTC_DATA_ATTR uint32_t seq = 0;
int16_t samples[N_SAMPLES];

struct Pending { uint32_t seq; String b64; };
Pending queue[QUEUE_N];
int qlen = 0;

void mpuInit() {
  Wire.begin();
  Wire.setClock(400000);
  Wire.beginTransmission(MPU_ADDR); Wire.write(0x6B); Wire.write(0x00); Wire.endTransmission();  // wake
  Wire.beginTransmission(MPU_ADDR); Wire.write(0x1C); Wire.write(0x08); Wire.endTransmission();  // +-4 g
  Wire.beginTransmission(MPU_ADDR); Wire.write(0x1A); Wire.write(0x01); Wire.endTransmission();  // DLPF 184 Hz
}

int16_t readAccelZ() {
  Wire.beginTransmission(MPU_ADDR); Wire.write(0x3F); Wire.endTransmission(false);
  Wire.requestFrom(MPU_ADDR, 2);
  return (int16_t)((Wire.read() << 8) | Wire.read());
}

void sample() {
  const uint32_t period_us = 1000000UL / SR_HZ;
  uint32_t next = micros();
  for (int i = 0; i < N_SAMPLES; i++) {
    while ((int32_t)(micros() - next) < 0) {}
    samples[i] = readAccelZ();
    next += period_us;
  }
}

String toBase64() {
  size_t outLen = 0;
  const size_t inLen = sizeof(samples);
  static unsigned char out[(sizeof(samples) * 4) / 3 + 8];
  mbedtls_base64_encode(out, sizeof(out), &outLen, (const unsigned char*)samples, inLen);
  return String((const char*)out).substring(0, outLen);
}

bool post(uint32_t s, const String& b64) {
  if (WiFi.status() != WL_CONNECTED) return false;
  HTTPClient http;
  http.begin(SERVER);
  http.addHeader("Content-Type", "application/json");
  String body = String("{\"device\":\"") + DEVICE + "\",\"seq\":" + s + ",\"machine\":\"" + MACHINE +
                "\",\"kind\":\"vibration\",\"sr\":" + SR_HZ + ",\"source\":\"live\",\"samples_b64\":\"" + b64 + "\"}";
  int code = http.POST(body);
  http.end();
  return code >= 200 && code < 300;   // 4xx means the server refused it; it would refuse a resend too
}

void flushQueue() {
  while (qlen > 0 && post(queue[0].seq, queue[0].b64)) {
    for (int i = 1; i < qlen; i++) queue[i - 1] = queue[i];
    qlen--;
  }
}

void setup() {
  Serial.begin(115200);
  mpuInit();
  WiFi.begin(WIFI_SSID, WIFI_PASS);
}

void loop() {
  sample();
  String b64 = toBase64();
  uint32_t s = seq++;
  flushQueue();
  if (qlen > 0 || !post(s, b64)) {
    if (qlen == QUEUE_N) {                       // full: drop the oldest, keep the newest
      for (int i = 1; i < QUEUE_N; i++) queue[i - 1] = queue[i];
      qlen--;
    }
    queue[qlen++] = {s, b64};
    Serial.printf("offline, queued %d\n", qlen);
  }
  delay((uint32_t)READ_EVERY_S * 1000UL);
}
