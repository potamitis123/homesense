#ifndef USB_AUDIO_H
#define USB_AUDIO_H

#include <stdio.h>
#include <string.h>
#include <stdbool.h>
#include <math.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/ringbuf.h"
#include "esp_log.h"
#include "esp_err.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "esp_system.h"

// USB Stream library
#include "usb/usb_host.h"  
#include "usb_stream.h"

// Include τα δικά σου headers
#include "global_heads.h"
#include "confing_file.h"

#ifdef __cplusplus
extern "C" {
#endif

// USB Audio configuration
#define USB_AUDIO_SAMPLE_RATE_DEFAULT    48000
#define USB_AUDIO_BITS_PER_SAMPLE        16
#define USB_AUDIO_CHANNELS               1

#ifndef MIN
#define MIN(a, b) ((a) < (b) ? (a) : (b))
#endif

#ifndef MAX
#define MAX(a, b) ((a) > (b) ? (a) : (b))
#endif
// Task parameters structure (ίδια με I2S)
typedef struct {
    RingbufHandle_t ring_buffer_handle;
    size_t max_item_size;
    size_t *read_size;
    esp_err_t status;
    Rec_config_file_t *rec_config_file;
} task_params_t_usb_audio;

// Function declarations - ίδιες με I2S όπου είναι δυνατόν
esp_err_t usb_audio_init(int record_time, uint8_t sample_bits);
void task_record_and_encode_audio_usb(void* pv_usb_audio_params);

// Audio processing functions (παρόμοιες με I2S)
int16_t highpass_filter_usb(int16_t input);

// Ενσωματωμένο downsampling και processing σε μία συνάρτηση
size_t process_usb_audio_data_with_downsampling(const int16_t* raw_samples_48k,
                                               const size_t samples_count_48k, 
                                               int16_t* processed_samples_16k, 
                                               double *rms);

// Utility functions
void time_message_usb(int64_t start_time, char *message);

// Status checking functions
bool usb_audio_is_connected(void);
bool usb_audio_is_streaming(void);

// Cleanup function
void usb_audio_cleanup(void);

#ifdef __cplusplus
}
#endif

#endif // USB_AUDIO_H