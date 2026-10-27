// Zero-parts RF test for an old VHF tube TV, classic ESP32 or ESP32-S3.
//
// D4 (GPIO4) drives a 13.33 MHz square wave (80 MHz APB / 6, exact, no
// fractional jitter) into a short jumper wire. Its 5th harmonic, 66.67 MHz,
// sits 0.58 MHz below channel 4's picture carrier (67.25 MHz), close enough
// for the fine tuning to reach. Hold the wire NEAR the antenna screws, never
// touching them: the TV has a hot chassis.
//
// (The first try used 40 MHz; the pad can't make sharp edges that fast, so
// its harmonics were too weak to see.)
//
// The carrier is switched on and off 120 times a second, nonstop. NTSC treats
// "carrier on" as black, so the screen shows two wide black bands with snow
// between them, drifting slowly up or down (there's no sync to lock them).

#include "driver/gpio.h"

const int RF_PIN = 4;
const uint32_t CARRIER_HZ = 13333333;
const uint32_t HALF_PERIOD_US = 4167;  // 120 Hz

void carrier(bool on) { ledcWrite(RF_PIN, on ? 1 : 0); }

void setup() {
  Serial.begin(115200);
  if (!ledcAttach(RF_PIN, CARRIER_HZ, 1)) Serial.println("ledcAttach failed");
  gpio_set_drive_capability((gpio_num_t)RF_PIN, GPIO_DRIVE_CAP_3);
  carrier(true);  // ledcReadFreq reports 0 while duty is 0
  Serial.printf("ledc freq readback: %u Hz\n", ledcReadFreq(RF_PIN));
  Serial.println("two black bands, nonstop");
}

void loop() {
  carrier(true);
  delayMicroseconds(HALF_PERIOD_US);
  carrier(false);
  delayMicroseconds(HALF_PERIOD_US);
}
