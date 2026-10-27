/* SPDX-License-Identifier: Apache-2.0 */
#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/i2s.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/sys/byteorder.h>
#include <zephyr/sys/crc.h>
#include <zephyr/sys/ring_buffer.h>
#include <zephyr/sys/atomic.h>
#include <zephyr/usb/usb_device.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>

#define RATE 16000
#define FRAMES 320
#define PCM_BYTES (FRAMES * sizeof(int16_t))
#define I2S_BYTES (FRAMES * 2 * sizeof(int32_t))
#define HEADER_BYTES 10
#define MAX_PAYLOAD 640
#define MIC 1
#define PLAY 2
#define TALK 3
#define STATUS 4
#define BUTTON 5

struct pcm_frame { int16_t samples[FRAMES]; uint8_t pressed; };
K_MEM_SLAB_DEFINE_STATIC(audio_slab, I2S_BYTES, 12, 4);
K_MSGQ_DEFINE(mic_queue, sizeof(struct pcm_frame), 8, 4);
K_MSGQ_DEFINE(play_queue, sizeof(struct pcm_frame), 8, 4);
RING_BUF_DECLARE(serial_rx, 8192);
K_SEM_DEFINE(rx_ready, 0, 1);
K_SEM_DEFINE(transport_ready, 0, 1);

static const struct device *const audio = DEVICE_DT_GET(DT_NODELABEL(i2s_rxtx));
static const struct device *const serial = DEVICE_DT_GET(DT_CHOSEN(zephyr_console));
static const struct gpio_dt_spec local_button = GPIO_DT_SPEC_GET(DT_ALIAS(sw0), gpios);
static const struct gpio_dt_spec led = GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);
static atomic_t remote_talk, mic_count, play_count, errors, dropped, peak16;
static atomic_t last_remote_ms, rx_overflow, crc_errors;
static int32_t previous_input, previous_output;

static bool speaking(void)
{
	if (atomic_get(&remote_talk) &&
	    (uint32_t)(k_uptime_get_32() - (uint32_t)atomic_get(&last_remote_ms)) > 1500) {
		atomic_clear(&remote_talk);
	}
	return atomic_get(&remote_talk) != 0;
}

static void uart_irq(const struct device *dev, void *unused)
{
	ARG_UNUSED(unused);
	if (!uart_irq_update(dev)) { return; }
	while (uart_irq_rx_ready(dev)) {
		uint8_t bytes[64];
		int count = uart_fifo_read(dev, bytes, sizeof(bytes));
		if (count <= 0) { break; }
		if (ring_buf_put(&serial_rx, bytes, count) != count) {
			atomic_inc(&rx_overflow);
		}
		k_sem_give(&rx_ready);
	}
}

static void accept_packet(uint8_t kind, const uint8_t *data, uint16_t len)
{
	if (kind == TALK && len == 1) {
		k_msgq_purge(&play_queue);
		atomic_set(&last_remote_ms, k_uptime_get_32());
		atomic_set(&remote_talk, data[0] != 0);
	} else if (kind == PLAY && len == PCM_BYTES && speaking()) {
		struct pcm_frame frame;
		for (int i = 0; i < FRAMES; i++) {
			frame.samples[i] = (int16_t)sys_get_le16(data + 2 * i);
		}
		atomic_set(&last_remote_ms, k_uptime_get_32());
		if (k_msgq_put(&play_queue, &frame, K_NO_WAIT) != 0) {
			atomic_inc(&dropped);
		} else {
			atomic_inc(&play_count);
		}
	}
}

static void serial_reader(void *a, void *b, void *c)
{
	uint8_t buffer[2048];
	size_t used = 0;
	for (;;) {
		k_sem_take(&rx_ready, K_FOREVER);
		used += ring_buf_get(&serial_rx, buffer + used, sizeof(buffer) - used);
		size_t pos = 0;
		while (used - pos >= HEADER_BYTES) {
			uint8_t *start = buffer + pos;
			uint16_t len = sys_get_le16(start + 8);
			if (memcmp(start, "CAT1", 4) || start[5] != 1 || len > MAX_PAYLOAD) {
				pos++; continue;
			}
			size_t total = HEADER_BYTES + len + 4;
			if (used - pos < total) { break; }
			if (crc32_ieee(start, HEADER_BYTES + len) ==
			    sys_get_le32(start + HEADER_BYTES + len)) {
				accept_packet(start[4], start + HEADER_BYTES, len);
				pos += total;
			} else {
				atomic_inc(&crc_errors); pos++;
			}
		}
		memmove(buffer, buffer + pos, used - pos);
		used -= pos;
		/* Continue draining even when an IRQ notification was coalesced. */
		if (!ring_buf_is_empty(&serial_rx)) { k_sem_give(&rx_ready); }
	}
}

static void send_packet(uint8_t kind, const void *payload, uint16_t len)
{
	static uint16_t sequence;
	uint8_t packet[HEADER_BYTES + MAX_PAYLOAD + 4];
	memcpy(packet, "CAT1", 4);
	packet[4] = kind; packet[5] = 1;
	sys_put_le16(sequence++, packet + 6);
	sys_put_le16(len, packet + 8);
	memcpy(packet + HEADER_BYTES, payload, len);
	sys_put_le32(crc32_ieee(packet, HEADER_BYTES + len), packet + HEADER_BYTES + len);
	for (int i = 0; i < HEADER_BYTES + len + 4; i++) { uart_poll_out(serial, packet[i]); }
}

static void serial_writer(void *a, void *b, void *c)
{
	struct pcm_frame frame;
	k_sem_take(&transport_ready, K_FOREVER);
	int64_t next_status = 0;
	bool was_pressed = false;
	for (;;) {
		if (k_msgq_get(&mic_queue, &frame, K_MSEC(100)) == 0) {
			if (frame.pressed != was_pressed) {
				send_packet(BUTTON, &frame.pressed, 1);
				was_pressed = frame.pressed;
			}
			if (frame.pressed) { send_packet(MIC, frame.samples, PCM_BYTES); }
		}
		if (k_uptime_get() >= next_status) {
			char json[320];
			int len = snprintf(json, sizeof(json),
				"{\"firmware\":\"CAT-App-Audio\",\"rate\":16000,\"mic_frames\":%ld,"
				"\"play_frames\":%ld,\"errors\":%ld,\"dropped\":%ld,\"peak\":%ld,"
				"\"talk\":%s,\"rx_overflow\":%ld,\"crc_errors\":%ld}",
				(long)atomic_get(&mic_count), (long)atomic_get(&play_count),
				(long)atomic_get(&errors), (long)atomic_get(&dropped),
				(long)atomic_get(&peak16), speaking() ? "true" : "false",
				(long)atomic_get(&rx_overflow), (long)atomic_get(&crc_errors));
			if (len > 0 && len < sizeof(json)) { send_packet(STATUS, json, len); }
			next_status = k_uptime_get() + 1000;
		}
	}
}
K_THREAD_DEFINE(rx_thread, 4096, serial_reader, NULL, NULL, NULL, 4, 0, 0);
K_THREAD_DEFINE(tx_thread, 4096, serial_writer, NULL, NULL, NULL, 5, 0, 0);

static int start_audio(void)
{
	for (int i = 0; i < 2; i++) {
		void *block;
		int ret = k_mem_slab_alloc(&audio_slab, &block, K_MSEC(100));
		if (ret < 0) { return ret; }
		memset(block, 0, I2S_BYTES);
		ret = i2s_write(audio, block, I2S_BYTES);
		if (ret < 0) { k_mem_slab_free(&audio_slab, block); return ret; }
	}
	previous_input = previous_output = 0;
	return i2s_trigger(audio, I2S_DIR_BOTH, I2S_TRIGGER_START);
}

int main(void)
{
	if (!device_is_ready(audio) || !device_is_ready(serial) ||
	    !gpio_is_ready_dt(&local_button) || !gpio_is_ready_dt(&led)) { return -ENODEV; }
	if (gpio_pin_configure_dt(&local_button, GPIO_INPUT) ||
	    gpio_pin_configure_dt(&led, GPIO_OUTPUT_INACTIVE)) { return -EIO; }
	if (usb_enable(NULL)) { return -EIO; }
	uint32_t dtr = 0;
	while (!dtr) {
		uart_line_ctrl_get(serial, UART_LINE_CTRL_DTR, &dtr);
		k_sleep(K_MSEC(50));
	}
	k_sem_give(&transport_ready);
	uart_irq_callback_user_data_set(serial, uart_irq, NULL);
	uart_irq_rx_enable(serial);
	struct i2s_config cfg = {
		.word_size = 32, .channels = 2, .format = I2S_FMT_DATA_FORMAT_I2S,
		.options = I2S_OPT_BIT_CLK_MASTER | I2S_OPT_FRAME_CLK_MASTER,
		.frame_clk_freq = RATE, .mem_slab = &audio_slab,
		.block_size = I2S_BYTES, .timeout = 1000,
	};
	int ret = i2s_configure(audio, I2S_DIR_BOTH, &cfg);
	if (ret || (ret = start_audio())) { atomic_inc(&errors); return ret; }
	uint32_t warmup = 5;
	bool button_state = false;
	uint8_t button_ticks = 0;
	for (;;) {
		void *block;
		size_t size;
		ret = i2s_read(audio, &block, &size);
		if (ret == 0) {
			struct pcm_frame mic = {0}, play = {0};
			bool remote = speaking();
			bool have_play = remote && k_msgq_get(&play_queue, &play, K_NO_WAIT) == 0;
			bool raw_button = !remote && gpio_pin_get_dt(&local_button) > 0;
			if (raw_button == button_state) { button_ticks = 0; }
			else if (++button_ticks >= 2) { button_state = raw_button; button_ticks = 0; }
			bool local = button_state && !remote;
			mic.pressed = local;
			gpio_pin_set_dt(&led, remote || local);
			int32_t *pcm = block;
			uint32_t max_sample = 0;
			for (size_t i = 0; i < MIN(size / 8, FRAMES); i++) {
				int32_t sample = pcm[2 * i] >> 8;
				int32_t filtered = sample - previous_input +
					(int32_t)(((int64_t)previous_output * 32260) / 32768);
				previous_input = sample; previous_output = filtered;
				int32_t value = CLAMP(filtered / 64, -32768, 32767);
				if (abs(value) > max_sample) { max_sample = abs(value); }
				mic.samples[i] = (!remote && !warmup) ? value : 0;
				int32_t output = have_play ? (int32_t)play.samples[i] * 16384 : 0;
				pcm[2 * i] = pcm[2 * i + 1] = output;
			}
			atomic_set(&peak16, max_sample);
			if (warmup) { warmup--; }
			if (k_msgq_put(&mic_queue, &mic, K_NO_WAIT)) { atomic_inc(&dropped); }
			atomic_inc(&mic_count);
			ret = i2s_write(audio, block, size);
			if (ret < 0) { k_mem_slab_free(&audio_slab, block); }
		}
		if (ret < 0) {
			atomic_inc(&errors);
			i2s_trigger(audio, I2S_DIR_BOTH, I2S_TRIGGER_DROP);
			k_sleep(K_MSEC(20));
			if (start_audio()) { return -EIO; }
			warmup = 5;
		}
	}
}
