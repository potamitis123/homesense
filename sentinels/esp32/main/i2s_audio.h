#ifndef I2S_AUDIO_H
#define I2S_AUDIO_H
//#include <stdint.h>
//#include <stdbool.h>
//#include <math.h>
#include <stdio.h>
#include <string.h>
#include "driver/i2s_std.h"
#include "driver/gpio.h"
#include "global_heads.h"
#include "confing_file.h"
#include "freertos/ringbuf.h"



#ifdef __cplusplus
extern "C" {
#endif

#define I2S_BCK_IO      12  // Bit Clock
#define I2S_WS_IO       11  // Word Select
#define I2S_DI_IO       10  // Data In
//#define SAMPLE_RATE     16000
//#define SAMPLE_BITS     32 
//#define RECORD_TIME     5   
//#define BUFFER_SIZE     (SAMPLE_RATE * RECORD_TIME * (SAMPLE_BITS/8))
#define WINDOW_SIZE     1024
//#define MIN_ACTIVE_WINDOWS 5
//#define ENERGY_THRESHOLD 10650

typedef struct {
    RingbufHandle_t ring_buffer_handle;
    size_t max_item_size;
    size_t *read_size;
    esp_err_t status;
    Rec_config_file_t *rec_config_file;
} task_params_t_i2s_audio;

void i2s_init(uint32_t sample_rate, uint8_t sample_bits);
void  task_record_and_encode_audio(void* pv_i2s_audio_params);

#ifdef __cplusplus
}
#endif
#endif