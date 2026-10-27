#ifndef GLOBAL_HEADS_H
#define GLOBAL_HEADS_H

#include <stdint.h>

//#define LOG_LOCAL_LEVEL ESP_LOG_DEBUG

//#define SAMPLE_RATE     16000
#define SAMPLE_BITS     32 
//#define RECORD_TIME     4   
//#define BUFFER_SIZE     (SAMPLE_RATE * RECORD_TIME * (SAMPLE_BITS/8))

#define MEM_STATISTICS 1
#define MP3 1
#define OPUS 0
#define RAW 2
#define WAV 3
#define ENCODER_TYPE WAV
#define MQTT_SEND 1
#define MQTT_TLS 1

// WiFi settings
#define WIFI_SSID       "your_wifi_ssid"
#define WIFI_PASS       "your_wifi_password"

typedef struct {
    char riff_header[4];      // "RIFF"
    uint32_t wav_size;        // Size of the entire file in bytes minus 8 bytes
    char wave_header[4];      // "WAVE"
    char fmt_header[4];       // "fmt "
    uint32_t fmt_chunk_size;  // Size of the format chunk (16 for PCM)
    uint16_t audio_format;    // Format code (1 for PCM)
    uint16_t num_channels;    // Number of channels (1 for mono)
    uint32_t sample_rate;     // Sample rate (e.g. 16000)
    uint32_t byte_rate;       // SampleRate * NumChannels * BitsPerSample/8
    uint16_t sample_alignment;// NumChannels * BitsPerSample/8
    uint16_t bit_depth;       // Bits per sample (e.g. 16)
    char data_header[4];      // "data"
    uint32_t data_size;       // Size of data section
} __attribute__((packed)) wav_header_t;

//********************************************************* */
#define DEVICE_ESP32S3_1 1
#define DEVICE_ESP32S3_2 2
#define DEVICE_ESP32S3_3 3
#define DEVICE_ESP32S3_4 4
#define DEVICE_ID DEVICE_ESP32S3_3

#define FIRSTBOOT 0

# define USB 0
# define I2S 1
#define MIC_TYPE I2S  //USB or I2S
//********************************************************* */

#if DEVICE_ID == DEVICE_ESP32S3_1
    #define MQTT_CLIENT_ID  "esp32s3-1"
    #define MQTT_USERNAME   "esp32s3-1"
    #define MQTT_PASSWORD   "your_mqtt_password"
#elif DEVICE_ID == DEVICE_ESP32S3_2
    #define MQTT_CLIENT_ID  "esp32s3-2"
    #define MQTT_USERNAME   "esp32s3-2"
    #define MQTT_PASSWORD   "your_mqtt_password"
#elif DEVICE_ID == DEVICE_ESP32S3_3
    #define MQTT_CLIENT_ID  "esp32s3-3"
    #define MQTT_USERNAME   "esp32s3-3"
    #define MQTT_PASSWORD   "your_mqtt_password"
#elif DEVICE_ID == DEVICE_ESP32S3_4
    #define MQTT_CLIENT_ID  "esp32s3-4"
    #define MQTT_USERNAME   "esp32s3-4"
    #define MQTT_PASSWORD   "your_mqtt_password"
#endif

#endif