// ESP32-S3 analog TV transmitter, VHF channel 4, no extra parts: the bench
// test for moving the station to an S3 (rf-tv, ported).
//
// The S3 has no APLL. Its I2S clocks are the 160 MHz PLL, the crystal or an
// external pin, and no whole-number division of those gives a bit rate whose
// pulse trains share harmonics 4.5 MHz apart (the classic's 27 MHz works
// because 27 = 6 x 4.5). So:
//
// Picture: I2S0 at R = 160 MHz / 6 = 26.667 Mbit/s exactly (MCLK 160/3, BCLK
// MCLK/2; no fractional divider, so no jitter). A pulse every other bit is a
// 13.333 MHz train; its 5th harmonic, 66.667 MHz, is 0.58 MHz under channel
// 4's 67.25 (the 13.33 MHz test showed up on the set, within fine tuning).
// Lines are 53 words = 1696 bits = 63.6 us, 262 lines = 60.01 Hz.
//
// Sound must sit 4.5 MHz up, at 71.1667 MHz. The 3-bit pattern 100 at R has
// its 8th harmonic at 71.111, 55.6 kHz short, so the pattern steps forward by
// 120 degrees every 5 words: one turn per 15 words (480 bits), which is
// exactly 55.6 kHz. The result repeats every 15 words and its strongest line is
// 71.1667 MHz (-1.6 dB against a clean carrier; the nearest spur is 167 kHz off
// at -8 dB, outside the set's 4.5 MHz sound filter). Simulated before it was
// written; see the S3 notes in the memory/README.
//
// Hold the wires on GPIO4 (picture) and GPIO5 (sound) NEAR the antenna screws,
// never touching them: the TV has a hot chassis.

#include "driver/i2s_std.h"
#include "esp_heap_caps.h"
#include "picture.h"

const int DOUT_PIN = 4, SOUND_PIN = 5;

const uint32_t BIT_RATE = 26666667;  // 160 MHz / 6
const float BITS_PER_US = BIT_RATE / 1e6f;
const int LINE_WORDS = 53;
const int LINE_BITS = LINE_WORDS * 32;  // 1696 bits = 63.6 us
const int SOUND_WORDS = 15 * 60;        // whole 15-word turns of the 55.6 kHz offset (3600 bytes, one DMA buffer)
const int LINES = 262;
const int VSYNC_LINES = 3;
const int FIRST_ACTIVE = 20, LAST_ACTIVE = 259;
const float SAFE = 0.90;     // fraction of the active area the picture fills
float dither = 0;            // peak-to-peak, in pulses; tunable over serial
const int MAX_FRAMES = 2;

const float HSYNC_US = 4.7, BACK_PORCH_US = 4.7, FRONT_PORCH_US = 1.5;
const float SYNC = 1.0, BLACK = 0.75;
float white = 0.125;  // tunable over serial, see loop()

i2s_chan_handle_t tx, sound_tx;
static uint32_t sound[SOUND_WORDS];
int sound_on = 1;
uint32_t *frame[MAX_FRAMES];
int nframes;
volatile uint32_t frames;  // sent, to check the real bit rate from loop()

// Carrier strength wanted at bit position `bit` of `line`.
float level(int line, int bit) {
  float t = (float)bit / BITS_PER_US;
  float line_us = (float)LINE_BITS / BITS_PER_US;
  if (line < VSYNC_LINES) return t < line_us - HSYNC_US ? SYNC : BLACK;
  if (t < HSYNC_US) return SYNC;
  float start = HSYNC_US + BACK_PORCH_US, end = line_us - FRONT_PORCH_US;
  if (line < FIRST_ACTIVE || line > LAST_ACTIVE || t < start || t >= end) return BLACK;
  float u = ((t - start) / (end - start) - 0.5f) / SAFE + 0.5f;
  float v = ((float)(line - FIRST_ACTIVE) / (LAST_ACTIVE + 1 - FIRST_ACTIVE) - 0.5f) / SAFE + 0.5f;
  if (u < 0 || u >= 1 || v < 0 || v >= 1) return BLACK;
  float lum = PICTURE[(int)(v * PIC_H) * PIC_W + (int)(u * PIC_W)] / 255.0f;
  return BLACK - lum * (BLACK - white);
}

uint32_t rng = 2463534242u;
float noise() {  // xorshift32, uniform in [-0.5, 0.5)
  rng ^= rng << 13;
  rng ^= rng >> 17;
  rng ^= rng << 5;
  return rng / 4294967296.0f - 0.5f;
}

void build_frame(uint32_t *frame) {
  for (int line = 0; line < LINES; line++) {
    uint32_t *words = frame + line * LINE_WORDS;
    memset(words, 0, LINE_WORDS * 4);
    float acc = 0;
    for (int bit = 0; bit + 1 < LINE_BITS; bit += 2) {
      float a = level(line, bit);
      bool picture = line >= FIRST_ACTIVE && line <= LAST_ACTIVE && a < BLACK;
      acc += picture ? a + dither * noise() : a;  // keep sync and blanking exact
      if (acc >= 1.0f) {
        acc -= 1.0f;
        words[bit / 32] |= 0x80000000u >> (bit % 32);  // I2S sends MSB first
      }
    }
  }
}

// The silent sound carrier: the 3-bit pattern 100, its phase stepped 120
// degrees every 5 words (+55.6 kHz at the 8th harmonic). Word w starts at bit
// 32w = 2w (mod 3), so its pulses sit on bits i = q - 2w (mod 3) of the word.
// "s2" steps the other way (71.056 MHz), to hear the difference.
void build_sound() {
  memset(sound, 0, sizeof(sound));
  if (!sound_on) return;
  int dir = sound_on == 2 ? -1 : 1;
  for (int w = 0; w < SOUND_WORDS; w++) {
    int q = ((dir * (w / 5)) % 3 + 3) % 3, r = ((q - 2 * w) % 3 + 3) % 3;
    for (int i = r; i < 32; i += 3) sound[w] |= 0x80000000u >> i;
  }
}

// Stereo x 32-bit slots: BCLK = 64 x sample rate = 26.667 MHz, MCLK = 128 x =
// 53.333 MHz = 160 MHz / 3 from the PLL (a whole divider: the driver's
// fraction comes out 0, so the clock is clean).
i2s_chan_handle_t start_i2s(i2s_port_t port, int pin) {
  i2s_chan_handle_t h;
  i2s_chan_config_t cc = I2S_CHANNEL_DEFAULT_CONFIG(port, I2S_ROLE_MASTER);
  cc.dma_desc_num = 8;
  cc.dma_frame_num = 500;  // 500 frames x 8 bytes = 4000 bytes, under the 4092 DMA limit
  ESP_ERROR_CHECK(i2s_new_channel(&cc, &h, NULL));
  i2s_std_config_t sc = {
    .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(BIT_RATE / 64),
    .slot_cfg = I2S_STD_MSB_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO),
    .gpio_cfg = {.mclk = I2S_GPIO_UNUSED, .bclk = I2S_GPIO_UNUSED, .ws = I2S_GPIO_UNUSED,
                 .dout = (gpio_num_t)pin, .din = I2S_GPIO_UNUSED, .invert_flags = {}},
  };
  sc.clk_cfg.clk_src = I2S_CLK_SRC_PLL_160M;
  sc.clk_cfg.mclk_multiple = I2S_MCLK_MULTIPLE_128;
  ESP_ERROR_CHECK(i2s_channel_init_std_mode(h, &sc));
  gpio_set_drive_capability((gpio_num_t)pin, GPIO_DRIVE_CAP_3);
  ESP_ERROR_CHECK(i2s_channel_enable(h));
  return h;
}

void setup() {
  Serial.begin(115200);
  for (; nframes < MAX_FRAMES; nframes++) {
    frame[nframes] = (uint32_t *)heap_caps_malloc(LINES * LINE_WORDS * 4, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (!frame[nframes]) break;
    build_frame(frame[nframes]);
  }
  if (!nframes) {
    Serial.println("frame alloc failed");
    return;
  }

  build_sound();
  tx = start_i2s(I2S_NUM_0, DOUT_PIN);
  sound_tx = start_i2s(I2S_NUM_1, SOUND_PIN);

  xTaskCreatePinnedToCore([](void *) {
    size_t w;
    for (;;) {
      i2s_channel_write(tx, frame[frames % nframes], LINES * LINE_WORDS * 4, &w, portMAX_DELAY);
      frames++;
    }
  }, "tx", 4096, NULL, configMAX_PRIORITIES - 1, NULL, 0);
  xTaskCreatePinnedToCore([](void *) {
    size_t w;
    for (;;) i2s_channel_write(sound_tx, sound, sizeof(sound), &w, portMAX_DELAY);
  }, "sound", 4096, NULL, configMAX_PRIORITIES - 1, NULL, 1);
  Serial.printf("transmitting on channel 4, %d frame copies, %u bytes heap left\n", nframes,
                heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
}

// Serial tuning: "d0.2" sets dither, "w0.2" sets the white level, "s0".."s2"
// picks the sound pattern; buffers are rebuilt in place (brief glitch).
void loop() {
  static uint32_t before = 0, last = 0;
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    float v = cmd.substring(1).toFloat();
    if (cmd[0] == 'd') dither = v;
    if (cmd[0] == 'w') white = v;
    if (cmd[0] == 's') sound_on = (int)v;  // 0 off, 1 on (71.167), 2 the wrong way (71.056)
    if (cmd[0] == 's') build_sound();
    else for (int i = 0; i < nframes; i++) build_frame(frame[i]);
    Serial.printf("dither %.2f white %.3f sound %s\n", dither, white, sound_on ? "on" : "off");
  }
  if (millis() - last >= 5000) {
    Serial.printf("%.1f frames/s (%.2f expected)\n", (frames - before) / ((millis() - last) / 1000.0f),
                  BIT_RATE / (float)(LINES * LINE_BITS));
    before = frames;
    last = millis();
  }
  delay(10);
}
