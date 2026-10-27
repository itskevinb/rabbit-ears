// ESP32 (classic) analog TV station: video + FM sound streamed from the Mac
// over USB serial, broadcast on VHF channel 4 with no extra parts.
//
// Same RF scheme as rf-tv (see its header), at R = 27 Mbit/s from the APLL:
//   picture  I2S0 -> D4: pulse train at R/2 = 13.5 MHz, 5th harmonic 67.5 MHz.
//            Pulses are dropped (first-order sigma-delta) to set the carrier
//            strength; NTSC negative modulation, non-interlaced 262 x 64 us.
//   sound    I2S1 -> D5: pulse train near R/3 = 9 MHz, 8th harmonic 72.0 MHz
//            (+4.5 MHz). FM: each pulse lands where an exact phase accumulator
//            says, so nudging the pulse spacing moves the 72 MHz line by up to
//            +/-25 kHz. The bit grid quantizes the timing; that error is
//            phase noise spread over megahertz, mostly outside the audio band.
// Both streams are generated live, one line (picture) or one audio sample
// (sound) at a time, on separate cores.
//
// Serial protocol (921600 baud, the CP2102's limit is ~1 Mbaud): the Mac
// sends 'H' and the board restarts clean (opening the port may or may not
// reset it) and prints "#ready". It then reports "#q N" (packet slots in use)
// whenever a slot frees and every 200 ms; the Mac keeps the queue topped up.
// (Single-byte credits didn't survive: the board-to-Mac line picks up RF junk
// from the transmitter wires, and some of it decodes as the credit byte.)
// Packet format:
//   A5 5A 56 31 | 6144 B picture (128x96, 4-bit, high nibble = left pixel)
//               | 781 B audio (int8, 7812.5 Hz = one sample per 2 lines)
// Video and audio play from the same packet, so they stay in sync: a packet's
// picture goes up when its first audio sample plays.
//
// Hold the wires NEAR the antenna screws, never touching: hot chassis.

#include "driver/i2s_std.h"

const uint32_t BIT_RATE = 27000000;
const int LINE_WORDS = 54, LINE_BITS = LINE_WORDS * 32;  // 1728 bits = 64.0 us
const int SLOTS = LINE_BITS / 2;                         // picture pulse slots per line
const int LINES = 262, VSYNC_LINES = 3, FIRST_ACTIVE = 20, LAST_ACTIVE = 259;
const float SLOTS_PER_US = BIT_RATE / 2 / 1e6f;
const float HSYNC_US = 4.7, BACK_PORCH_US = 4.7, FRONT_PORCH_US = 1.5, SAFE = 0.90;
const int SYNC = 256, BLACK = 192, WHITE = 32;  // carrier strength, 256 = every pulse

const int VID_W = 128, VID_H = 96, VID_BYTES = VID_W * VID_H / 2;
const int AUD_PER_PKT = 781;
const int NQ = 4;
const int BATCH = 8;  // picture lines per I2S write

const int SOUND_BLOCK_BITS = 2 * LINE_BITS;  // one audio sample
const int SOUND_BLOCK_WORDS = SOUND_BLOCK_BITS / 32;
const uint32_t SOUND_STEP0 = 3u << 20;  // pulse spacing, bits in 12.20 fixed point

struct Packet {
  uint8_t pic[VID_BYTES];
  int8_t audio[AUD_PER_PKT];
};
Packet queue[NQ];
volatile bool full[NQ];
volatile int showing = -1;  // packet whose picture is on screen

i2s_chan_handle_t pic_tx, sound_tx;
// Picture lines run one sigma-delta pass over the slots: level = pal[src[slot]],
// where pal[0..127] holds this line's pixel levels and the last two entries
// are sync and black. Blank and vsync lines never change, so they're
// prebuilt.
const int PAL_SYNC = VID_W, PAL_BLACK = VID_W + 1;
uint8_t slot_src[SLOTS];
int16_t row_of_line[LINES];  // picture row for a line, -1 outside
uint32_t blank_line[LINE_WORDS], vsync_line[LINE_WORDS];
uint32_t sound_step[256];    // pulse spacing per audio sample value

// Fast path for picture lines (the slot-by-slot pass took ~92% of a core and
// sometimes missed the deadline, which drops the carrier and breaks sync).
// Each pixel covers a run of 5 or 6 slots at one level, and the sigma-delta
// state is just acc (0..255), so a table gives the bits a pixel makes and the
// acc it leaves: 128 lookups per line instead of ~860 slot steps. The part of
// the line before the first pixel always starts from acc = 0, so it's
// prebuilt too.
const int RUN_MIN = 5, RUN_MAX = 6;
uint32_t run_lut[16][256][RUN_MAX - RUN_MIN + 1];  // bits << 8 | new acc
uint8_t run_len[VID_W];
int pic_end_slot, prefix_words, prefix_tail_bits, prefix_acc;
uint32_t prefix[LINE_WORDS], prefix_tail;

i2s_chan_handle_t start_i2s(i2s_port_t port, int pin) {
  i2s_chan_handle_t h;
  i2s_chan_config_t cc = I2S_CHANNEL_DEFAULT_CONFIG(port, I2S_ROLE_MASTER);
  cc.dma_desc_num = 8;
  cc.dma_frame_num = 500;
  ESP_ERROR_CHECK(i2s_new_channel(&cc, &h, NULL));
  i2s_std_config_t sc = {
    .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(BIT_RATE / 64),
    .slot_cfg = I2S_STD_MSB_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO),
    .gpio_cfg = {.mclk = I2S_GPIO_UNUSED, .bclk = I2S_GPIO_UNUSED, .ws = I2S_GPIO_UNUSED,
                 .dout = (gpio_num_t)pin, .din = I2S_GPIO_UNUSED, .invert_flags = {}},
  };
  sc.clk_cfg.clk_src = I2S_CLK_SRC_APLL;
  sc.clk_cfg.mclk_multiple = I2S_MCLK_MULTIPLE_128;
  ESP_ERROR_CHECK(i2s_channel_init_std_mode(h, &sc));
  gpio_set_drive_capability((gpio_num_t)pin, GPIO_DRIVE_CAP_3);
  ESP_ERROR_CHECK(i2s_channel_enable(h));
  return h;
}

void sigma_delta(const uint8_t *src, const int *pal, uint32_t *w) {
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

void build_tables() {
  float start = HSYNC_US + BACK_PORCH_US, end = SLOTS / SLOTS_PER_US - FRONT_PORCH_US;
  uint8_t vsync_src[SLOTS];
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
        run_lut[v][a0][len - RUN_MIN] = bits << 8 | a;
      }
  // 72 MHz = 8 x pulse rate, so a deviation d moves the pulse rate by d/8.
  for (int i = 0; i < 256; i++) {
    float dev = (int8_t)i / 128.0f * 25000.0f;
    sound_step[i] = (uint32_t)(3.0f * (1 << 20) / (1.0f + dev / 72e6f) + 0.5f);
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

void IRAM_ATTR fast_picture_line(const uint8_t *rp, uint32_t *w) {
  memcpy(w, prefix, prefix_words * 4);
  int nw = prefix_words, nbits = prefix_tail_bits, acc = prefix_acc;
  uint64_t bits = prefix_tail;
  for (int x = 0; x < VID_W; x++) {
    int v = (x & 1) ? rp[x >> 1] & 15 : rp[x >> 1] >> 4;
    int len = run_len[x];
    uint32_t e = run_lut[v][acc][len - RUN_MIN];
    bits = (bits << (2 * len)) | (e >> 8);
    nbits += 2 * len;
    acc = e & 255;
    if (nbits >= 32) {
      nbits -= 32;
      w[nw++] = bits >> nbits;
    }
  }
  for (int s = pic_end_slot; s < SLOTS; s++) {  // front porch and right margin, at black
    acc += BLACK;
    int bit = acc >> 8;
    acc -= bit << 8;
    bits = (bits << 2) | (bit << 1);
    nbits += 2;
    if (nbits >= 32) {
      nbits -= 32;
      w[nw++] = bits >> nbits;
    }
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
  for (int t = 0; t < 50; t++) {
    for (int i = 0; i < VID_W / 2; i++) row[i] = esp_random();
    slow_picture_line(row, a);
    fast_picture_line(row, b);
    bad += memcmp(a, b, sizeof(a)) != 0;
  }
  Serial.printf("#selftest %s (%d of 50 rows differ)\n", bad ? "fail" : "ok", bad);
}

volatile uint32_t pic_busy_us, sound_busy_us;  // compute time, for the load report in loop()

void picture_task(void *) {
  static uint32_t buf[BATCH * LINE_WORDS];
  for (;;) {
    const uint8_t *pic = showing >= 0 ? queue[showing].pic : NULL;  // latched per field
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
  uint32_t pos = 0;  // next pulse, bits from block start, 12.20 fixed point
  int playing = -1, idx = 0, next = 0;
  for (;;) {
    if (playing < 0 && full[next]) {  // start, or resume after running dry
      playing = next;
      idx = 0;
      show(playing);
    }
    int8_t sample = playing >= 0 ? queue[playing].audio[idx] : 0;
    uint32_t step = sound_step[(uint8_t)sample];
    uint32_t t0 = micros();
    memset(buf, 0, sizeof(buf));
    while (pos < ((uint32_t)SOUND_BLOCK_BITS << 20)) {
      uint32_t b = pos >> 20;
      buf[b >> 5] |= 0x80000000u >> (b & 31);
      pos += step;
    }
    pos -= SOUND_BLOCK_BITS << 20;
    sound_busy_us += micros() - t0;
    size_t w;
    i2s_channel_write(sound_tx, buf, sizeof(buf), &w, portMAX_DELAY);
    if (playing >= 0 && ++idx == AUD_PER_PKT) {
      next = (playing + 1) % NQ;
      playing = -1;  // picked up at the top of the loop as soon as it's full
    }
  }
}

void setup() {
  Serial.setRxBufferSize(32768);
  Serial.begin(921600);
  build_tables();
  self_test();
  pic_tx = start_i2s(I2S_NUM_0, 4);
  sound_tx = start_i2s(I2S_NUM_1, 5);
  xTaskCreatePinnedToCore(picture_task, "pic", 4096, NULL, configMAX_PRIORITIES - 1, NULL, 0);
  xTaskCreatePinnedToCore(sound_task, "sound", 4096, NULL, configMAX_PRIORITIES - 2, NULL, 1);
  Serial.println("#ready");
}

// Receive packets into free slots, in order. Every 5 s, report how busy the
// two generators are.
void loop() {
  static uint32_t last = millis(), last_q = 0;
  if (millis() - last_q >= 200) {
    report_queue();
    last_q = millis();
  }
  if (millis() - last >= 5000) {
    uint32_t span = (millis() - last) * 10;  // percent of the span, in us/100
    Serial.printf("#load picture %u%% sound %u%%\n", pic_busy_us / span, sound_busy_us / span);
    pic_busy_us = sound_busy_us = 0;
    last = millis();
  }
  static int fill = 0;
  static const uint8_t magic[4] = {0xA5, 0x5A, 'V', '1'};
  static int matched = 0;
  while (Serial.available()) {
    if (matched < 4) {
      int c = Serial.read();
      if (c == 'H' && matched == 0) ESP.restart();
      matched = c == magic[matched] ? matched + 1 : (c == magic[0] ? 1 : 0);
      continue;
    }
    if (full[fill]) return;  // the Mac ran ahead; the packet waits in the RX buffer
    size_t got = Serial.readBytes((uint8_t *)&queue[fill], sizeof(Packet));
    matched = 0;
    if (got == sizeof(Packet)) {
      full[fill] = true;
      fill = (fill + 1) % NQ;
    }
  }
  delay(1);
}
