// Bench check for the I2S bitstream transmitter: runs I2S slowly, reads the
// BCLK and DOUT pads back by polling, and prints the bits it saw, so we can
// confirm the on-wire order of the 32-bit words before going to 40 MHz.

#include "driver/i2s_std.h"
#include "soc/gpio_reg.h"
#include "soc/gpio_periph.h"

const int BCLK_PIN = 18, WS_PIN = 19, DOUT_PIN = 4;
i2s_chan_handle_t tx;
static uint32_t buf[1000];

void setup() {
  Serial.begin(115200);
  delay(500);
  const uint32_t pat[4] = {0x00000000, 0xFFFFFFFF, 0x12345678, 0x9ABCDEF0};
  for (int i = 0; i < 1000; i++) buf[i] = pat[i % 4];

  i2s_chan_config_t cc = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
  ESP_ERROR_CHECK(i2s_new_channel(&cc, &tx, NULL));
  i2s_std_config_t sc = {
    .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(600),
    .slot_cfg = I2S_STD_MSB_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO),
    .gpio_cfg = {.mclk = I2S_GPIO_UNUSED, .bclk = (gpio_num_t)BCLK_PIN, .ws = (gpio_num_t)WS_PIN,
                 .dout = (gpio_num_t)DOUT_PIN, .din = I2S_GPIO_UNUSED, .invert_flags = {}},
  };
  sc.clk_cfg.clk_src = I2S_CLK_SRC_PLL_160M;
  sc.clk_cfg.mclk_multiple = I2S_MCLK_MULTIPLE_1152;
  ESP_ERROR_CHECK(i2s_channel_init_std_mode(tx, &sc));
  size_t w;
  i2s_channel_preload_data(tx, buf, sizeof(buf), &w);
  ESP_ERROR_CHECK(i2s_channel_enable(tx));
  xTaskCreatePinnedToCore([](void *) {
    size_t w;
    for (;;) i2s_channel_write(tx, buf, sizeof(buf), &w, portMAX_DELAY);
  }, "feed", 4096, NULL, 5, NULL, 0);

  REG_SET_BIT(GPIO_PIN_MUX_REG[BCLK_PIN], FUN_IE);
  REG_SET_BIT(GPIO_PIN_MUX_REG[DOUT_PIN], FUN_IE);
  delay(200);

  static char bits[513];
  uint32_t last = 0;
  for (int n = 0; n < 512;) {
    uint32_t in = REG_READ(GPIO_IN_REG);
    uint32_t clk = (in >> BCLK_PIN) & 1;
    if (clk && !last) bits[n++] = ((in >> DOUT_PIN) & 1) ? '1' : '0';
    last = clk;
  }
  bits[512] = 0;
  Serial.println("BITS");
  Serial.println(bits);
  Serial.println("END");
}

void loop() { delay(1000); }
