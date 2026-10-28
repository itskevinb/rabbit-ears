// ESP32 (classic) analog TV station: video + FM sound streamed over Wi-Fi
// (TCP port 5000, mDNS tubetv.local) or USB serial, broadcast on VHF
// channel 4 with no extra parts.
//
// Same RF scheme as rf-tv (see its header), at R = 27 Mbit/s from the APLL:
//   picture  I2S0 -> D4: pulse train at R/2 = 13.5 MHz, 5th harmonic 67.5 MHz.
//            Pulses are dropped (first-order sigma-delta) to set the carrier
//            strength; NTSC negative modulation, non-interlaced 262 x 64 us.
//   sound    I2S1 -> D5: 3-bit frames at R/3 = 9 MHz whose 8th-harmonic image
//            is 72.0 MHz (+4.5 MHz). One bit moves the 72 MHz phase by 240
//            degrees, so the 6 one- and two-pulse frames (100 110 010 011 001
//            101) give phases in 60-degree steps. FM (+/-25 kHz) = a phase
//            accumulator, quantized to those steps with first-order error
//            feedback, which pushes the error away from the audio band.
//            Simulated: audio SNR ~70 dB. On the real set it was worse: loud
//            garbled sound. The shaped error lands between the sound and
//            picture carriers, and the set's intercarrier detector mixes it
//            with the sound carrier straight into the 4.5 MHz sound IF, which
//            the simulation didn't model. Commands pick the method: 'O' first
//            method (one pulse per cycle at the nearest bit: distorted), 'N'
//            this one, 'W'/'X'/'Y' per-word (see make_word_sound_block); 'Y'
//            is the default and the best on the real set.
// Both streams are generated live, one line (picture) or one audio sample
// (sound) at a time, on separate cores.
//
// Stream format, same on both links: everything is framed by A5 5A, either
//   "C" + one letter: a command (see receive())
//   "V2" + uint16 LE n | 24576 B picture (256x192, 4-bit, high nibble = left
//        pixel) | n B audio (mu-law, 15625 Hz = one sample per line; n is
//        15625 / fps, so any frame rate works)
//   "V3" + uint16 LE n + uint16 LE L | L B zlib-compressed picture | n B audio
//        Wi-Fi uses this: the Arduino build's TCP window is fixed at 5.7 KB,
//        which with a Wi-Fi round trip caps raw throughput at ~430 KB/s
//        (measured; 24 fps raw needs ~600). Frames deflate 3-4x, and the
//        ESP32 has inflate in ROM.
// Video and audio play from the same packet, so they stay in sync: a packet's
// picture goes up when its first audio sample plays.
//
// Wi-Fi: TCP's own flow control paces the sender (the board stops reading
// when its 4 packet slots are full). A new connection takes over and flushes
// the queue.
// USB (921600 baud, the CP2102's limit is ~1 Mbaud, so ~3 fps at 256x192): command H
// restarts the board clean (opening the port may or may not reset it), and
// it prints "#ready". It then reports "#q N" (slots in use) whenever a slot
// frees and every 200 ms; the Mac keeps the queue topped up. (Single-byte
// credits didn't survive: RF from the transmitter wires puts junk on the
// serial lines, and some of it decoded as the credit byte.)
//
// Over-the-air updates (tools/ota.sh) when wifi_secrets.h defines OTA_PASSWORD:
// ArduinoOTA on UDP 3232, password-protected, by IP (our own mDNS stays as is).
// An update drops the stream first, for the heap, and reboots into the new build.
//
// ESP32-S3 (built when the target is an S3, e.g. Freenove N16R8 with octal
// PSRAM): there's no APLL, so R = 160 MHz / 6 = 26.667 Mbit/s, the picture is
// the 5th harmonic of 13.333 MHz (66.667 MHz, in the set's fine tuning) and
// lines are 53 words (63.6 us, 60.01 Hz). No whole-number plan puts the sound
// 4.5 MHz up, so the per-word FM carries a +55.6 kHz offset: its quiet carrier
// steps 120 degrees every 5 words, a 15-word cycle whose strongest line is
// exactly 71.1667 MHz (bench-tested in rf-s3: quiet; off = buzz; the wrong way =
// hiss). Audio is one sample per line, so it runs at 15723 Hz, 0.6% fast:
// everything plays 0.6% quick, which nobody hears. Packets live in PSRAM, 8 of
// them. The S3 keeps to the per-word methods (O and N are ignored).
//
// Hold the wires NEAR the antenna screws, never touching: hot chassis.

#include <ArduinoOTA.h>
#include <ESPmDNS.h>
#include <WiFi.h>
#include "driver/i2s_std.h"
#include "miniz.h"  // ESP32 ROM inflate
#include "wifi_secrets.h"  // WIFI_SSID, WIFI_PSK; not committed

#if CONFIG_IDF_TARGET_ESP32S3
const uint32_t BIT_RATE = 26666667;                       // 160 MHz / 6
const int LINE_WORDS = 53, LINE_BITS = LINE_WORDS * 32;  // 1696 bits = 63.6 us
const float SOUND_OFFSET_HZ = 55555.6f;  // 3-bit pattern's 8th harmonic (71.111) to picture + 4.5 MHz (71.1667)
const int WORD_EVERY[3] = {1, 5, 5};      // decide every n words: must divide 5 for a clean quiet carrier
#define I2S_SOURCE I2S_CLK_SRC_PLL_160M
#else
const uint32_t BIT_RATE = 27000000;
const int LINE_WORDS = 54, LINE_BITS = LINE_WORDS * 32;  // 1728 bits = 64.0 us
const float SOUND_OFFSET_HZ = 0;          // 72.0 MHz is exactly picture + 4.5 MHz
const int WORD_EVERY[3] = {1, 2, 4};
#define I2S_SOURCE I2S_CLK_SRC_APLL
#endif
const int SLOTS = LINE_BITS / 2;                         // picture pulse slots per line
const int LINES = 262, VSYNC_LINES = 3, FIRST_ACTIVE = 20, LAST_ACTIVE = 259;
const float SLOTS_PER_US = BIT_RATE / 2 / 1e6f;
const float HSYNC_US = 4.7, BACK_PORCH_US = 4.7, FRONT_PORCH_US = 1.5, SAFE = 0.90;
const int SYNC = 256, BLACK = 192, WHITE = 32;  // carrier strength, 256 = every pulse

const int VID_W = 256, VID_H = 192, VID_BYTES = VID_W * VID_H / 2;
const int AUD_MAX = 800;  // audio samples per packet (24 fps needs 651; USB's ~3 fps would need 5208, so USB is for commands now)
#if CONFIG_IDF_TARGET_ESP32S3
const int NQ = 8;  // in PSRAM: a third of a second of slack against Wi-Fi hiccups
#else
const int NQ = 3;  // 26 KB each; with Wi-Fi up, 4 don't fit
#endif
const int BATCH = 8;  // picture lines per I2S write

const int SOUND_BLOCK_BITS = LINE_BITS;  // one audio sample
const int SOUND_BLOCK_WORDS = SOUND_BLOCK_BITS / 32;
const uint32_t SOUND_STEP0 = 3u << 20;  // pulse spacing, bits in 12.20 fixed point

struct Packet {
  uint8_t pic[VID_BYTES];
  uint8_t audio[AUD_MAX];  // mu-law (G.711): ~13-bit quiet-passage quality in 8 bits
  int n_audio;
};
Packet *queue[NQ];  // on the heap (PSRAM on the S3): 3 x 26 KB is too much for static DRAM
volatile bool full[NQ];
volatile bool stream_reset;  // set by loop() for a new connection; the sound task empties the queue
volatile int showing = -1;  // packet whose picture is on screen

i2s_chan_handle_t pic_tx, sound_tx;
TaskHandle_t pic_task_h, sound_task_h;
volatile bool ota_busy;  // an update is running: the generators are stopped and the queue is freed
// Picture lines run one sigma-delta pass over the slots: level = pal[src[slot]],
// where pal[0..VID_W-1] holds this line's pixel levels and the last two entries
// are sync and black. Blank and vsync lines never change, so they're
// prebuilt.
const int PAL_SYNC = VID_W, PAL_BLACK = VID_W + 1;
uint16_t slot_src[SLOTS];
int16_t row_of_line[LINES];  // picture row for a line, -1 outside
uint32_t blank_line[LINE_WORDS], vsync_line[LINE_WORDS];
uint32_t sound_step[256];    // old method: pulse spacing per audio sample value
uint32_t sound_dphase[256];  // new method: phase step per frame, 2^32 = one turn
const uint32_t PHASE60 = 715827883;  // 2^32 / 6
const uint8_t PHASE_PATTERN[6] = {0b100, 0b110, 0b010, 0b011, 0b001, 0b101};  // 0, 60 .. 300 deg
uint8_t q_of_top[256];  // nearest 60-degree step, from the top 8 bits of (phase + 30 degrees)
uint32_t phase_of_q[6];
// Sound method: 0 = one pulse per cycle at the nearest bit, 1 = noise-shaped
// 6-phase pairs, 2..4 = per-word (see make_word_sound_block), deciding every
// 1, 2 or 4 words.
volatile int sound_mode = 4;
volatile bool line_dither = true;
const uint32_t PHASE120 = 1431655765;  // 2^32 / 3
uint32_t word_pattern[3];              // pulses on bits i = r (mod 3) of a word, MSB first
uint32_t word_dphase[256];             // phase step per 32-bit word

// Fast path for picture lines (the slot-by-slot pass took ~92% of a core and
// sometimes missed the deadline, which drops the carrier and breaks sync).
// Each pixel covers a run of 5 or 6 slots at one level, and the sigma-delta
// state is just acc (0..255), so a table gives the bits a pixel makes and the
// acc it leaves: 128 lookups per line instead of ~860 slot steps. The part of
// the line before the first pixel always starts from acc = 0, so it's
// prebuilt too.
const int RUN_MIN = 2, RUN_MAX = 3;
uint16_t run_lut[(RUN_MAX - RUN_MIN + 1) << 12];  // [len - RUN_MIN][grey][acc] -> bits << 8 | new acc
uint16_t run_key[VID_W];                          // (run_len - RUN_MIN) << 12, per pixel
uint8_t run_len[VID_W];
int pic_end_slot, prefix_words, prefix_tail_bits, prefix_acc;
uint32_t prefix[LINE_WORDS], prefix_tail;

i2s_chan_handle_t start_i2s(i2s_port_t port, int pin) {
  i2s_chan_handle_t h;
  i2s_chan_config_t cc = I2S_CHANNEL_DEFAULT_CONFIG(port, I2S_ROLE_MASTER);
  cc.dma_desc_num = 8;
  cc.dma_frame_num = 128;  // 8 x 1024 B = ~2.4 ms of slack; more leaves too little heap for Wi-Fi at 256x192
  ESP_ERROR_CHECK(i2s_new_channel(&cc, &h, NULL));
  i2s_std_config_t sc = {
    .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(BIT_RATE / 64),
    .slot_cfg = I2S_STD_MSB_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO),
    .gpio_cfg = {.mclk = I2S_GPIO_UNUSED, .bclk = I2S_GPIO_UNUSED, .ws = I2S_GPIO_UNUSED,
                 .dout = (gpio_num_t)pin, .din = I2S_GPIO_UNUSED, .invert_flags = {}},
  };
  sc.clk_cfg.clk_src = I2S_SOURCE;
  sc.clk_cfg.mclk_multiple = I2S_MCLK_MULTIPLE_128;
  ESP_ERROR_CHECK(i2s_channel_init_std_mode(h, &sc));
  gpio_set_drive_capability((gpio_num_t)pin, GPIO_DRIVE_CAP_3);
  ESP_ERROR_CHECK(i2s_channel_enable(h));
  return h;
}

void sigma_delta(const uint16_t *src, const int *pal, uint32_t *w) {
  int acc = 0;
  uint32_t word = 0;
  for (int s = 0; s < SLOTS; s++) {
    acc += pal[src[s]];
    int bit = acc >> 8;  // acc < 512, so this is 0 or 1
    acc -= bit << 8;
    word = (word << 2) | (bit << 1);  // "10": pulse on the slot's first bit
    if ((s & 15) == 15) w[s >> 4] = word;
  }
}

int ulaw_to_linear(uint8_t u) {  // G.711, +/-32124
  u = ~u;
  int t = (((u & 0x0F) << 3) + 0x84) << ((u >> 4) & 7);
  return u & 0x80 ? 0x84 - t : t - 0x84;
}

void build_tables() {
  float start = HSYNC_US + BACK_PORCH_US, end = SLOTS / SLOTS_PER_US - FRONT_PORCH_US;
  uint16_t vsync_src[SLOTS];
  for (int s = 0; s < SLOTS; s++) {
    float t = s / SLOTS_PER_US;
    float u = ((t - start) / (end - start) - 0.5f) / SAFE + 0.5f;
    slot_src[s] = t < HSYNC_US ? PAL_SYNC : (t >= start && t < end && u >= 0 && u < 1) ? (int)(u * VID_W) : PAL_BLACK;
    vsync_src[s] = t < SLOTS / SLOTS_PER_US - HSYNC_US ? PAL_SYNC : PAL_BLACK;
  }
  int pal[VID_W + 2];
  for (int i = 0; i < VID_W + 2; i++) pal[i] = BLACK;
  pal[PAL_SYNC] = SYNC;
  sigma_delta(slot_src, pal, blank_line);
  sigma_delta(vsync_src, pal, vsync_line);
  for (int l = 0; l < LINES; l++) {
    float v = ((float)(l - FIRST_ACTIVE) / (LAST_ACTIVE + 1 - FIRST_ACTIVE) - 0.5f) / SAFE + 0.5f;
    row_of_line[l] = (l >= FIRST_ACTIVE && l <= LAST_ACTIVE && v >= 0 && v < 1) ? (int)(v * VID_H) : -1;
  }
  int first = 0;
  while (slot_src[first] >= VID_W) first++;
  pic_end_slot = first;
  for (int x = 0; x < VID_W; x++) {
    run_len[x] = 0;
    while (pic_end_slot < SLOTS && slot_src[pic_end_slot] == x) run_len[x]++, pic_end_slot++;
    if (run_len[x] < RUN_MIN || run_len[x] > RUN_MAX) Serial.printf("#bad run %d at pixel %d\n", run_len[x], x);
    run_key[x] = (run_len[x] - RUN_MIN) << 12;
  }
  int acc = 0;
  uint32_t word = 0;
  for (int s = 0; s < first; s++) {
    acc += pal[slot_src[s]];
    int bit = acc >> 8;
    acc -= bit << 8;
    word = (word << 2) | (bit << 1);
    if ((s & 15) == 15) prefix[s >> 4] = word;
  }
  prefix_words = first / 16;
  prefix_tail_bits = (first % 16) * 2;
  prefix_tail = word & ((1ull << prefix_tail_bits) - 1);
  prefix_acc = acc;
  for (int v = 0; v < 16; v++)
    for (int a0 = 0; a0 < 256; a0++)
      for (int len = RUN_MIN; len <= RUN_MAX; len++) {
        int a = a0;
        uint32_t bits = 0;
        for (int i = 0; i < len; i++) {
          a += BLACK - v * (BLACK - WHITE) / 15;
          int bit = a >> 8;
          a -= bit << 8;
          bits = (bits << 2) | (bit << 1);
        }
        run_lut[(len - RUN_MIN) << 12 | v << 8 | a0] = bits << 8 | a;
      }
  // 72 MHz = 8 x pulse rate, so a deviation d moves the pulse rate by d/8.
  for (int i = 0; i < 256; i++) {
    float dev = ulaw_to_linear(i) / 32768.0f * 25000.0f;  // tables are indexed by the mu-law code
    sound_step[i] = (uint32_t)(3.0f * (1 << 20) / (1.0f + dev / 72e6f) + 0.5f);
    sound_dphase[i] = (uint32_t)(int32_t)(dev / 9e6f * 4294967296.0f);
    word_dphase[i] = (uint32_t)(int32_t)((dev + SOUND_OFFSET_HZ) * 32 / BIT_RATE * 4294967296.0f);
  }
  for (int i = 0; i < 256; i++) q_of_top[i] = i * 6 / 256;
  for (int r = 0; r < 3; r++) {
    word_pattern[r] = 0;
    for (int i = r; i < 32; i += 3) word_pattern[r] |= 0x80000000u >> i;
  }
  for (int q = 0; q < 6; q++) {
    phase_of_q[q] = q * PHASE60;
  }
}

// The reference: one sigma-delta step per slot. Used by the self-test.
void slow_picture_line(const uint8_t *rp, uint32_t *w) {
  int pal[VID_W + 2];
  for (int x = 0; x < VID_W; x += 2) {
    pal[x] = BLACK - (rp[x >> 1] >> 4) * (BLACK - WHITE) / 15;
    pal[x + 1] = BLACK - (rp[x >> 1] & 15) * (BLACK - WHITE) / 15;
  }
  pal[PAL_SYNC] = SYNC;
  pal[PAL_BLACK] = BLACK;
  sigma_delta(slot_src, pal, w);
}

// Append nb (<= 6) bits to the line, MSB first, through a 32-bit word.
#define PUSH(val, nb)                                         \
  do {                                                        \
    uint32_t v_ = (val);                                      \
    int nb_ = (nb);                                           \
    if (n + nb_ < 32) {                                       \
      word = (word << nb_) | v_;                              \
      n += nb_;                                               \
    } else {                                                  \
      int spill_ = n + nb_ - 32;                              \
      w[nw++] = (word << (nb_ - spill_)) | (v_ >> spill_);    \
      word = v_ & ((1u << spill_) - 1);                       \
      n = spill_;                                             \
    }                                                         \
  } while (0)

void IRAM_ATTR fast_picture_line(const uint8_t *rp, uint32_t *w) {
  memcpy(w, prefix, prefix_words * 4);
  // A random start for the sigma-delta on each line, new every field, turns the
  // fixed fine stripes into grain that changes 60 times a second.
  static uint32_t rng = 1;
  rng = rng * 1664525u + 1013904223u;
  int nw = prefix_words, n = prefix_tail_bits, acc = line_dither ? rng >> 24 : prefix_acc;
  uint32_t word = prefix_tail;
  for (int x = 0; x < VID_W; x += 2) {
    uint32_t pair = rp[x >> 1];
    uint32_t e = run_lut[run_key[x] | (pair >> 4) << 8 | acc];
    PUSH(e >> 8, 2 * run_len[x]);
    e = run_lut[run_key[x + 1] | (pair & 15) << 8 | (e & 255)];
    PUSH(e >> 8, 2 * run_len[x + 1]);
    acc = e & 255;
  }
  for (int s = pic_end_slot; s < SLOTS; s++) {  // front porch and right margin, at black
    acc += BLACK;
    int bit = acc >> 8;
    acc -= bit << 8;
    PUSH(bit << 1, 2);
  }
}

void make_line(int line, const uint8_t *pic, uint32_t *w) {
  int row = row_of_line[line];
  if (line < VSYNC_LINES) memcpy(w, vsync_line, sizeof(vsync_line));
  else if (row < 0 || !pic) memcpy(w, blank_line, sizeof(blank_line));
  else fast_picture_line(pic + row * (VID_W / 2), w);
}

// Check the fast path against the reference on random rows.
void self_test() {
  uint8_t row[VID_W / 2];
  uint32_t a[LINE_WORDS], b[LINE_WORDS];
  int bad = 0;
  line_dither = false;  // the reference starts every line from the same state
  for (int t = 0; t < 50; t++) {
    for (int i = 0; i < VID_W / 2; i++) row[i] = esp_random();
    slow_picture_line(row, a);
    fast_picture_line(row, b);
    bad += memcmp(a, b, sizeof(a)) != 0;
  }
  line_dither = true;
  Serial.printf("#selftest %s (%d of 50 rows differ)\n", bad ? "fail" : "ok", bad);
}

volatile uint32_t pic_busy_us, sound_busy_us;  // compute time, for the load report in loop()

void picture_task(void *) {
  static uint32_t buf[BATCH * LINE_WORDS];
  for (;;) {
    const uint8_t *pic = showing >= 0 ? queue[showing]->pic : NULL;  // latched per field
    for (int l = 0; l < LINES; l += BATCH) {
      int n = min(BATCH, LINES - l);
      uint32_t t0 = micros();
      for (int i = 0; i < n; i++) make_line(l + i, pic, buf + i * LINE_WORDS);
      pic_busy_us += micros() - t0;
      size_t w;
      i2s_channel_write(pic_tx, buf, n * LINE_WORDS * 4, &w, portMAX_DELAY);
    }
  }
}

void report_queue() {
  int n = 0;
  for (int i = 0; i < NQ; i++) n += full[i];
  Serial.printf("#q %d\n", n);
}

// New-method sound for one audio sample: 1152 frames of 3 bits, decided in
// pairs (the same pattern twice), so 4.5 million decisions a second. Deciding
// every frame took more than the whole core and starved the serial link;
// pairs simulate the same (~70 dB audio SNR) with slightly less leakage into
// the picture.
void IRAM_ATTR make_sound_block(uint32_t dphase, uint32_t *buf, uint32_t &phase, uint32_t &err) {
  uint32_t ph = phase, e = err, acc = 0;
  int n = 0, nw = 0;
  uint32_t dpair = dphase * 2;
  for (int k = 0; k < SOUND_BLOCK_BITS / 6; k++) {
    ph += dpair;
    uint32_t u = ph - e;  // wanted phase, minus last pair's error
    uint32_t q = q_of_top[(u + PHASE60 / 2) >> 24];
    e = phase_of_q[q] - u;
    uint32_t pat = PHASE_PATTERN[q] * 9;  // the 3-bit pattern twice: p << 3 | p
    if (n <= 26) {
      acc = (acc << 6) | pat;
      n += 6;
      if (n == 32) buf[nw++] = acc, acc = 0, n = 0;
    } else {  // the pair straddles two words
      int spill = n + 6 - 32;
      buf[nw++] = (acc << (6 - spill)) | (pat >> spill);
      acc = pat & ((1u << spill) - 1);
      n = spill;
    }
  }
  phase = ph;
  err = e;
}

// Per-word sound: the pulses stay one bit wide, one every 3 bits, and only
// their offset in the 3-bit grid changes (bit n's 72 MHz phase is 120*n deg,
// so offset o = phase 120*o). The offset is re-decided every `every` words
// with first-order error feedback, which pushes the timing error out to
// ~400 kHz (every word = 844k decisions/s): above the audio, and, with
// single-bit pulses and few decisions, mostly clear of the picture. Simulated
// ~43 dB audio SNR to 7 kHz vs ~2 dB for method 0.
void make_word_sound_block(uint32_t dword, int every, uint32_t *buf, uint32_t &phase, uint32_t &err, int &q,
                           int &grid, int &count) {
  uint32_t ph = phase, e = err;
  // grid (bit 0 of this word, mod 3) and count (words since the last decision)
  // carry over between blocks: a block isn't always a multiple of 3 bits (1696
  // on the S3) or of `every` words (54 or 53), and restarting either at each
  // block glitched the quiet carrier's steps once a line.
  for (int w = 0; w < SOUND_BLOCK_WORDS; w++) {
    if (count >= every) count = 0;  // `every` changed
    if (count++ == 0) {
      ph += dword * every;
      uint32_t u = ph - e;
      q = ((u + PHASE120 / 2) >> 8) * 3 >> 24;
      e = q * PHASE120 - u;
    }
    buf[w] = word_pattern[(q - grid + 3) % 3];
    grid = (grid + 2) % 3;  // 32 = 2 (mod 3)
  }
  phase = ph;
  err = e;
}

// Put packet n's picture up; the one it replaces is off screen, so free it.
void show(int n) {
  int old = showing;
  showing = n;
  if (old >= 0 && old != n) {
    full[old] = false;
    report_queue();
  }
}

void sound_task(void *) {
  static uint32_t buf[SOUND_BLOCK_WORDS];
  uint32_t pos = 0;  // old method: next pulse, bits from block start, 12.20 fixed point
  uint32_t phase = 0, err = 0;  // method 1
  uint32_t wphase = 0, werr = 0;  // per-word methods
  int wq = 0, wgrid = 0, wcount = 0;
  int playing = -1, idx = 0, next = 0;
  for (;;) {
    if (stream_reset) {
      playing = -1, next = 0;
      showing = -1;
      for (int i = 0; i < NQ; i++) full[i] = false;
      stream_reset = false;
    }
    if (playing < 0 && full[next]) {  // start, or resume after running dry
      playing = next;
      idx = 0;
      show(playing);
    }
    uint8_t sample = playing >= 0 ? queue[playing]->audio[idx] : 0xFF;  // 0xFF = mu-law zero
    uint32_t t0 = micros();
    if (sound_mode >= 2) {
      make_word_sound_block(word_dphase[(uint8_t)sample], WORD_EVERY[sound_mode - 2], buf, wphase, werr, wq, wgrid, wcount);
    } else if (sound_mode == 1) {
      make_sound_block(sound_dphase[(uint8_t)sample], buf, phase, err);
    } else {
      uint32_t step = sound_step[(uint8_t)sample];
      memset(buf, 0, sizeof(buf));
      while (pos < ((uint32_t)SOUND_BLOCK_BITS << 20)) {
        uint32_t b = pos >> 20;
        buf[b >> 5] |= 0x80000000u >> (b & 31);
        pos += step;
      }
      pos -= SOUND_BLOCK_BITS << 20;
    }
    sound_busy_us += micros() - t0;
    size_t w;
    i2s_channel_write(sound_tx, buf, sizeof(buf), &w, portMAX_DELAY);
    if (playing >= 0 && ++idx >= queue[playing]->n_audio) {
      next = (playing + 1) % NQ;
      playing = -1;  // picked up at the top of the loop as soon as it's full
    }
  }
}

void setup() {
  Serial.setRxBufferSize(1024);  // USB carries commands; Wi-Fi carries the stream
  Serial.begin(921600);
#if CONFIG_IDF_TARGET_ESP32S3
  for (int i = 0; i < NQ; i++) queue[i] = (Packet *)heap_caps_malloc(sizeof(Packet), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
#else
  for (int i = 0; i < NQ; i++) queue[i] = (Packet *)malloc(sizeof(Packet));
#endif
  build_tables();
  self_test();
  pic_tx = start_i2s(I2S_NUM_0, 4);
  sound_tx = start_i2s(I2S_NUM_1, 5);
  // Sound (~82%) gets core 0 to itself; picture (~60%) shares core 1 with
  // loop(), which receives the serial stream.
  xTaskCreatePinnedToCore(sound_task, "sound", 4096, NULL, configMAX_PRIORITIES - 1, &sound_task_h, 0);
  xTaskCreatePinnedToCore(picture_task, "pic", 4096, NULL, configMAX_PRIORITIES - 2, &pic_task_h, 1);
  WiFi.mode(WIFI_STA);
  WiFi.setHostname("tubetv");
  WiFi.setSleep(false);  // power save adds tens of ms of latency and kills throughput
  WiFi.begin(WIFI_SSID, WIFI_PSK);
  if (xTaskCreatePinnedToCore(network_task, "net", 8192, NULL, 5, NULL, 0) != pdPASS) Serial.println("#net task failed");
  Serial.println("#ready");
}

WiFiServer server(5000);
WiFiClient client;
int fill = 0;

// Compressed picture passes through here into the packet slot.
const int ZBUF = 1024;  // compressed input is streamed through this
static uint8_t zbuf[ZBUF];
volatile uint32_t inflate_us;

// Speed test (command T): read and discard for 3 s, then report KB/s and
// signal strength on both links.
template <typename Link> void throughput_test(Link &in) {
  uint32_t start = millis(), bytes = 0;
  while (millis() - start < 3000) {
    int r = in.available() ? in.read(zbuf, sizeof(zbuf)) : 0;
    if (r > 0) bytes += r;
    else vTaskDelay(1);
  }
  char msg[80];
  snprintf(msg, sizeof(msg), "#tput %u KB/s rssi %d dBm\n", bytes / 3 / 1024, WiFi.RSSI());
  Serial.print(msg);
  in.print(msg);
}


// Status for the station (command S), one line back on the same link: how
// long since boot and why it last reset (a crash or brownout vs a Wi-Fi drop),
// memory, signal, the generators' load and how often Wi-Fi has (re)joined.
volatile uint32_t pic_pct, sound_pct, inflate_pct, wifi_joins;

const char *reset_name() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "power-on";
    case ESP_RST_EXT: return "reset-pin";
    case ESP_RST_SW: return "software";
    case ESP_RST_PANIC: return "crash";
    case ESP_RST_INT_WDT: return "interrupt-watchdog";
    case ESP_RST_TASK_WDT: return "task-watchdog";
    case ESP_RST_WDT: return "watchdog";
    case ESP_RST_DEEPSLEEP: return "deep-sleep";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_SDIO: return "sdio";
    default: return "unknown";
  }
}

template <typename Link> void send_status(Link &out) {
  char msg[160];
  snprintf(msg, sizeof(msg), "#status up %lu reset %s heap %u minheap %u rssi %d load %u/%u/%u joins %u\n",
           (unsigned long)(millis() / 1000), reset_name(), ESP.getFreeHeap(), ESP.getMinFreeHeap(), WiFi.RSSI(),
           pic_pct, sound_pct, inflate_pct, wifi_joins);
  out.print(msg);
}


// Bulk read with a 1 s timeout. Stream::readBytes goes a byte at a time, which
// held Wi-Fi to ~20 fps at 256x192.
template <typename Link> bool read_full(Link &in, uint8_t *buf, size_t n) {
  size_t got = 0;
  uint32_t start = millis();
  while (got < n && millis() - start < 1000) {
    int r = in.available() ? in.read(buf + got, n - got) : 0;
    if (r > 0) got += r;
    else vTaskDelay(1);
  }
  return got == n;
}

// Read framed data from one link into free packet slots, in order. Returns
// when the link has nothing more or the queue is full (for TCP, the unread
// data then holds the sender back).
template <typename Link> void receive(Link &in, int &matched) {
  static bool compressed;
  if (ota_busy) return;  // the packet slots are gone
  uint32_t start = millis();
  while (in.available()) {
    // Hand the core back now and then: with the S3's 8 slots and faster link
    // the data never ran out, the loop never returned, and core 0's idle task
    // starved (task watchdog). Between packets, so nothing is cut in half.
    if (millis() - start > 20) return;
    if (matched != 4) {  // 1 = saw A5, 2 = A5 5A, 3 = "V", 4 = "V2", 5 = "C"
      int c = in.read();
      if (matched == 5) {
        if (c == 'H') ESP.restart();
#if !CONFIG_IDF_TARGET_ESP32S3  // the old methods assume the 27 MHz grid
        if (c == 'O') sound_mode = 0;
        if (c == 'N') sound_mode = 1;
#endif
        if (c >= 'W' && c <= 'Y') sound_mode = 2 + (c - 'W');
        if (c == 'D' || c == 'd') line_dither = c == 'D';
        if (c == 'T') throughput_test(in);
        if (c == 'S') send_status(in);
        matched = 0;
      } else if (matched == 2 && c == 'C') matched = 5;
      else if (matched == 2 && c == 'V') matched = 3;
      else if (matched == 3 && (c == '2' || c == '3')) matched = 4, compressed = c == '3';
      else if (matched == 0 && c == 0xA5) matched = 1;
      else if (matched == 1 && c == 0x5A) matched = 2;
      else matched = c == 0xA5 ? 1 : 0;
      continue;
    }
    if (full[fill]) return;
    Packet &p = *queue[fill];
    uint16_t n = 0, zlen = 0;
    matched = 0;
    if (!read_full(in, (uint8_t *)&n, 2) || n > AUD_MAX) continue;
    if (compressed) {
      if (!read_full(in, (uint8_t *)&zlen, 2)) continue;
      // Inflate as it arrives, ZBUF bytes at a time (a whole-frame buffer
      // left the Wi-Fi driver too little heap for its receive buffers).
      static tinfl_decompressor inflator;  // ~11 KB; static, not on the task stack
      tinfl_init(&inflator);
      tinfl_status st = TINFL_STATUS_NEEDS_MORE_INPUT;
      size_t produced = 0, left = zlen;
      bool ok = true;
      while (left > 0 && ok && st != TINFL_STATUS_DONE) {
        size_t chunk = left < ZBUF ? left : ZBUF, off = 0;
        if (!read_full(in, zbuf, chunk)) ok = false;
        left -= chunk;
        uint32_t t0 = micros();
        while (ok && off < chunk) {
          size_t in_len = chunk - off, out_len = VID_BYTES - produced;
          st = tinfl_decompress(&inflator, zbuf + off, &in_len, p.pic, p.pic + produced, &out_len,
                                TINFL_FLAG_PARSE_ZLIB_HEADER | TINFL_FLAG_USING_NON_WRAPPING_OUTPUT_BUF |
                                    (left ? TINFL_FLAG_HAS_MORE_INPUT : 0));
          off += in_len;
          produced += out_len;
          if (st < 0 || st == TINFL_STATUS_DONE || (in_len == 0 && out_len == 0)) break;
        }
        inflate_us += micros() - t0;
        if (st < 0) ok = false;
      }
      if (!ok || st != TINFL_STATUS_DONE || produced != VID_BYTES) continue;
    } else if (!read_full(in, p.pic, VID_BYTES)) continue;
    if (!read_full(in, p.audio, n)) continue;
    p.n_audio = n;
    full[fill] = true;
    fill = (fill + 1) % NQ;
  }
}

// Receive packets into free slots, in order. Every 5 s, report how busy the
// two generators are.
// Wi-Fi side, on core 0 next to the network stack: core 1 is mostly busy
// building the picture, and receiving from there capped the stream at ~20 fps.
void network_task(void *) {
  bool serving = false, joined = false;
  int matched = 0;
  for (;;) {
    bool up = WiFi.status() == WL_CONNECTED;
    if (up && !joined) wifi_joins++;
    joined = up;
    if (!serving && WiFi.status() == WL_CONNECTED) {
      MDNS.begin("tubetv");
      MDNS.addService("tubetv", "tcp", 5000);
      server.begin();
      server.setNoDelay(true);
#ifdef OTA_PASSWORD
      ArduinoOTA.setHostname("tubetv");
      ArduinoOTA.setMdnsEnabled(false);  // MDNS is already up, as tubetv
      ArduinoOTA.setPassword(OTA_PASSWORD);
      // An update arrives faster than flash erases go, and with the stream's
      // ~100 KB of packet slots in use the received data ran the heap out
      // (a crash at 43%). So stop the generators and free the slots first;
      // there's no going back after that, so an error reboots the old build.
      ArduinoOTA.onStart([] {
        Serial.println("#ota start");
        ota_busy = true;
        if (client) client.stop();
        // Suspend, don't i2s_channel_disable(): a task parked inside
        // i2s_channel_write holds the channel lock, and disable waits on it
        // forever. The DMA just keeps cycling its last buffers.
        vTaskSuspend(pic_task_h);
        vTaskSuspend(sound_task_h);
        for (int i = 0; i < NQ; i++) {
          free(queue[i]);
          queue[i] = NULL;
        }
        Serial.printf("#ota heap %u\n", ESP.getFreeHeap());
      });
      ArduinoOTA.onError([](ota_error_t e) {
        Serial.printf("#ota error %u\n", e);
        delay(100);
        ESP.restart();
      });
      ArduinoOTA.begin();
#endif
      serving = true;
      Serial.printf("#wifi %s\n", WiFi.localIP().toString().c_str());
    }
#ifdef OTA_PASSWORD
    if (serving) ArduinoOTA.handle();  // an update runs to the end in here, then reboots
#endif
    if (serving && server.hasClient()) {  // a new stream takes over
      client = server.available();
      client.setNoDelay(true);
      stream_reset = true;
      while (stream_reset) vTaskDelay(1);
      fill = 0;
      matched = 0;
    }
    if (client && client.connected()) receive(client, matched);
    vTaskDelay(1);
  }
}

void loop() {
  static uint32_t last = millis(), last_q = 0;
  if (millis() - last_q >= 200) {
    report_queue();
    last_q = millis();
  }
  if (millis() - last >= 5000) {
    uint32_t span = (millis() - last) * 10;  // percent of the span, in us/100
    pic_pct = pic_busy_us / span, sound_pct = sound_busy_us / span, inflate_pct = inflate_us / span;
    Serial.printf("#load picture %u%% sound %u%% inflate %u%% heap %u wifi %s\n", pic_busy_us / span,
                  sound_busy_us / span, inflate_us / span, ESP.getFreeHeap(), WiFi.localIP().toString().c_str());
    pic_busy_us = sound_busy_us = inflate_us = 0;
    last = millis();
  }
  static int usb_matched = 0;
  receive(Serial, usb_matched);
  delay(1);
}
