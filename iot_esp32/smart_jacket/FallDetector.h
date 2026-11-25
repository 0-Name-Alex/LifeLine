// FallDetector.h - on-device fall detection for the Smart Jacket (ESP32 + MPU6050).
// Pure C++ (no Arduino calls) so the same logic can be unit-tested on a PC.
//
// Call update() at a steady 50-100 Hz with raw MPU6050 readings:
//   accel in m/s^2, gyro in rad/s (Adafruit_MPU6050 getEvent() units), time in ms.
//
// Detection = free fall (optional) -> impact -> lying + quiet. A fall takes ~0.5-1.5 s and the
// impact spike lasts ~50-200 ms, which is why this must run on the ESP32 and not on 1 Hz MQTT data.
#pragma once
#include <math.h>
#include <stdint.h>

class FallDetector {
 public:
  // Tunables (start here, then tune with recorded trials).
  float freefallG = 0.5f;          // |a| below this = free fall
  uint32_t freefallMinMs = 60;     // ...for at least this long
  float impactG = 2.5f;            // |a| peak = impact
  uint32_t impactWindowMs = 1000;  // impact must follow free fall within this
  uint32_t settleMs = 1000;        // ignore bouncing right after impact
  uint32_t postCheckMs = 2000;     // then check posture for this long
  float lyingTiltDeg = 60.0f;      // torso angle from upright = lying
  float quietGyroDps = 40.0f;      // lying AND rotating less than this = not getting up
  float quietFraction = 0.7f;      // share of post-check samples that must be lying + quiet
  uint32_t recoverMs = 3000;       // upright this long clears a latched fall

  enum State { IDLE, FREEFALL, IMPACT, FALLEN };

  // Gravity direction when the worker stands upright. Fixed, no calibration:
  // default (0, 0, 1) = MPU6050 z axis pointing UP (accel_z reads about +9.8 when standing).
  FallDetector(float upX = 0, float upY = 0, float upZ = 1) : ux_(upX), uy_(upY), uz_(upZ) {
    normalize(ux_, uy_, uz_);
  }

  // Returns true exactly once when a new fall is detected.
  bool update(float ax, float ay, float az, float gx, float gy, float gz, uint32_t nowMs) {
    const float aMag = sqrtf(ax * ax + ay * ay + az * az);
    const float g = aMag / 9.80665f;
    const float gyroDps = sqrtf(gx * gx + gy * gy + gz * gz) * 57.29578f;
    if (g > peakG_) peakG_ = g;
    if (g < minG_) minG_ = g;
    if (gyroDps > peakGyro_) peakGyro_ = gyroDps;
    lastTilt_ = aMag > 0.1f ? tiltDeg(ax / aMag, ay / aMag, az / aMag) : lastTilt_;

    switch (state_) {
      case IDLE:
        if (g < freefallG) {
          if (freefallStart_ == 0) freefallStart_ = nowMs ? nowMs : 1;
          if (nowMs - freefallStart_ >= freefallMinMs) { state_ = FREEFALL; stateMs_ = nowMs; }
        } else {
          freefallStart_ = 0;
        }
        // Trips and slips often have no clean free-fall phase; a hard impact alone also starts a check.
        if (g >= impactG) startImpact(nowMs, g);
        return false;

      case FREEFALL:
        if (g >= impactG) startImpact(nowMs, g);
        else if (nowMs - stateMs_ > impactWindowMs) { state_ = IDLE; freefallStart_ = 0; }
        return false;

      case IMPACT: {
        if (g > impactPeakG_) impactPeakG_ = g;
        uint32_t since = nowMs - stateMs_;
        if (since < settleMs) return false;
        postSamples_++;
        if (lastTilt_ >= lyingTiltDeg && gyroDps < quietGyroDps && fabsf(g - 1.0f) < 0.3f) postHits_++;
        if (since < settleMs + postCheckMs) return false;
        bool fell = postSamples_ > 0 && postHits_ >= quietFraction * postSamples_;
        freefallStart_ = 0;
        if (fell) { state_ = FALLEN; stateMs_ = nowMs; uprightSince_ = 0; fallCount_++; return true; }
        state_ = IDLE;  // impact but person still upright/moving (jump, bump, dropped tool)
        return false;
      }

      case FALLEN:  // latched until the worker is upright again for recoverMs
        if (lastTilt_ < lyingTiltDeg - 20.0f) {
          if (uprightSince_ == 0) uprightSince_ = nowMs ? nowMs : 1;
          if (nowMs - uprightSince_ >= recoverMs) { state_ = IDLE; freefallStart_ = 0; }
        } else {
          uprightSince_ = 0;
        }
        return false;
    }
    return false;
  }

  bool isFallen() const { return state_ == FALLEN; }
  State state() const { return state_; }
  float tilt() const { return lastTilt_; }
  float lastImpactG() const { return impactPeakG_; }
  uint32_t fallCount() const { return fallCount_; }

  // Peak/min since the last call; call once per MQTT publish (1 s) and send these values.
  void takeSummary(float &peakG, float &minG, float &peakGyroDps) {
    peakG = peakG_; minG = minG_; peakGyroDps = peakGyro_;
    peakG_ = 0; minG_ = 100; peakGyro_ = 0;
  }

  // Change the fixed upright direction, e.g. setUpright(0, -1, 0) if accel_y reads -9.8 standing.
  void setUpright(float x, float y, float z) {
    ux_ = x; uy_ = y; uz_ = z; normalize(ux_, uy_, uz_);
  }

 private:
  State state_ = IDLE;
  uint32_t stateMs_ = 0, freefallStart_ = 0, uprightSince_ = 0, fallCount_ = 0;
  float ux_, uy_, uz_;  // upright gravity direction (unit), set in the constructor
  int postSamples_ = 0, postHits_ = 0;
  float lastTilt_ = 0, impactPeakG_ = 0;
  float peakG_ = 0, minG_ = 100, peakGyro_ = 0;

  void startImpact(uint32_t nowMs, float g) {
    state_ = IMPACT; stateMs_ = nowMs; impactPeakG_ = g; postSamples_ = postHits_ = 0;
  }
  float tiltDeg(float x, float y, float z) const {
    float d = x * ux_ + y * uy_ + z * uz_;
    d = d > 1.0f ? 1.0f : (d < -1.0f ? -1.0f : d);
    return acosf(d) * 57.29578f;
  }
  static void normalize(float &x, float &y, float &z) {
    float n = sqrtf(x * x + y * y + z * z);
    if (n > 0) { x /= n; y /= n; z /= n; }
  }
};
