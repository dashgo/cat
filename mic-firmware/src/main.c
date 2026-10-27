/* SPDX-License-Identifier: Apache-2.0 */
#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/i2s.h>
#include <zephyr/logging/log.h>
#include <stdint.h>
#include <string.h>

LOG_MODULE_REGISTER(mic_monitor, LOG_LEVEL_INF);

/* INMP441 requires 64 BCLK edges per stereo frame: two 32-bit slots. */
#define RATE 48000
#define FRAMES 240
#define BLOCK_BYTES (FRAMES * 2 * sizeof(int32_t))
#define LIMIT24 (1 << 20)
#define RAMP_FRAMES 240
K_MEM_SLAB_DEFINE_STATIC(audio_slab, BLOCK_BYTES, 12, 4);

static const struct device *const audio = DEVICE_DT_GET(DT_NODELABEL(i2s_rxtx));
static const struct gpio_dt_spec talk = GPIO_DT_SPEC_GET(DT_ALIAS(sw0), gpios);
static const struct gpio_dt_spec led = GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);

/* Visible in the debugger as well as periodic serial logs. */
volatile uint32_t mic_blocks, mic_peak, mic_nonzero, stream_errors;
static int32_t previous_input, previous_output;
static uint32_t ramp;

static int start_stream(void)
{
	for (int i = 0; i < 2; i++) {
		void *block;
		int ret = k_mem_slab_alloc(&audio_slab, &block, K_MSEC(100));
		if (ret < 0) {
			return ret;
		}
		memset(block, 0, BLOCK_BYTES);
		ret = i2s_write(audio, block, BLOCK_BYTES);
		if (ret < 0) {
			k_mem_slab_free(&audio_slab, block);
			return ret;
		}
	}
	previous_input = previous_output = 0;
	ramp = 0;
	return i2s_trigger(audio, I2S_DIR_BOTH, I2S_TRIGGER_START);
}

static void process_audio(int32_t *pcm, size_t bytes, bool enabled,
			  uint32_t *peak, uint32_t *nonzero)
{
	for (size_t i = 0; i + 1 < bytes / sizeof(*pcm); i += 2) {
		/* L/R tied to GND selects left; the valid 24 bits are MSB-aligned. */
		int32_t sample = pcm[i] >> 8;
		uint32_t magnitude = sample < 0 ? -sample : sample;
		if (magnitude > *peak) {
			*peak = magnitude;
		}
		*nonzero += sample != 0;

		/* DC blocker, approximately 38 Hz at 48 kHz. */
		int32_t filtered = sample - previous_input +
			(int32_t)(((int64_t)previous_output * 32604) / 32768);
		previous_input = sample;
		previous_output = filtered;
		int64_t amplified = (int64_t)filtered * 4;
		amplified = CLAMP(amplified, -LIMIT24, LIMIT24 - 1);
		if (enabled && ramp < RAMP_FRAMES) {
			ramp++;
		} else if (!enabled && ramp > 0) {
			ramp--;
		}
		int32_t output = (int32_t)(amplified * ramp / RAMP_FRAMES);
		/* Multiplication avoids undefined signed left shift for negatives. */
		pcm[i] = output * 256;
		pcm[i + 1] = pcm[i];
	}
}

int main(void)
{
	if (!device_is_ready(audio) || !gpio_is_ready_dt(&talk) ||
	    !gpio_is_ready_dt(&led)) {
		LOG_ERR("I2S/button/LED not ready");
		return -ENODEV;
	}
	int ret = gpio_pin_configure_dt(&talk, GPIO_INPUT);
	if (ret < 0) {
		return ret;
	}
	ret = gpio_pin_configure_dt(&led, GPIO_OUTPUT_INACTIVE);
	if (ret < 0) {
		return ret;
	}
	struct i2s_config config = {
		.word_size = 32,
		.channels = 2,
		.format = I2S_FMT_DATA_FORMAT_I2S,
		.options = I2S_OPT_BIT_CLK_MASTER | I2S_OPT_FRAME_CLK_MASTER,
		.frame_clk_freq = RATE,
		.mem_slab = &audio_slab,
		.block_size = BLOCK_BYTES,
		.timeout = 1000,
	};
	ret = i2s_configure(audio, I2S_DIR_BOTH, &config);
	if (ret < 0) {
		LOG_ERR("Full duplex configure failed: %d", ret);
		return ret;
	}
	ret = start_stream();
	if (ret < 0) {
		LOG_ERR("Start failed: %d", ret);
		return ret;
	}
	LOG_INF("INMP441 monitor: 48k/32-bit stereo, SDIN=P1.14; hold Button 1 to talk");
	int64_t ready_at = k_uptime_get() + 100;
	int64_t next_log = k_uptime_get() + 2000;
	uint32_t window_peak = 0, window_nonzero = 0;
	for (;;) {
		void *block;
		size_t bytes;
		ret = i2s_read(audio, &block, &bytes);
		if (ret == 0) {
			int pressed = gpio_pin_get_dt(&talk);
			bool enabled = pressed > 0 && k_uptime_get() >= ready_at;
			gpio_pin_set_dt(&led, enabled);
			process_audio(block, bytes, enabled, &window_peak, &window_nonzero);
			mic_blocks++;
			ret = i2s_write(audio, block, bytes);
			if (ret < 0) {
				k_mem_slab_free(&audio_slab, block);
			}
		}
		if (ret < 0) {
			stream_errors++;
			LOG_ERR("Stream failure %d; restarting", ret);
			gpio_pin_set_dt(&led, 0);
			/* DROP stops DMA and releases driver-owned queued buffers. */
			i2s_trigger(audio, I2S_DIR_BOTH, I2S_TRIGGER_DROP);
			k_sleep(K_MSEC(20));
			ret = start_stream();
			if (ret < 0) {
				LOG_ERR("Restart failed: %d", ret);
				return ret;
			}
			ready_at = k_uptime_get() + 100;
		}
		if (k_uptime_get() >= next_log) {
			mic_peak = window_peak;
			mic_nonzero = window_nonzero;
			LOG_INF("blocks=%u peak24=%u nonzero=%u errors=%u talk=%u",
				mic_blocks, mic_peak, mic_nonzero, stream_errors, ramp > 0);
			window_peak = window_nonzero = 0;
			next_log = k_uptime_get() + 2000;
		}
	}
}
