#ifndef MQTT_CLIENT_H
#define MQTT_CLIENT_H

#include <stdbool.h>
#include "esp_system.h"
#include "mqtt_client.h"
#include "global_heads.h"
#include "confing_file.h"

//#define MQTT_BROKER     "your-broker.example.com"
//#define MQTT_PORT       1883
//#define MQTT_TOPIC      "audio/recordings/esp32s3-2"
//#define MQTT_CLIENT_ID  "esp32s3-2"
//#define MQTT_USERNAME   "esp32s3-2"
//#define MQTT_PASSWORD   "your_mqtt_password"
#define CHUNK_SIZE      6144

#define MQTT_NOT_CONNECTED_BIT BIT0
#define MQTT_CONNECTED_BIT BIT1

#define MQTT_TOPIC      "audio/recordings"

// Presence/status topic: audio/status/<mqtt_client_id>. Retained "online" is published
// on connect; retained "offline" is set as the broker's Last Will (LWT) so the server
// sees the device go offline even on an ungraceful disconnect (power loss, WiFi drop).
#define MQTT_STATUS_TOPIC   "audio/status"
#define MQTT_STATUS_ONLINE  "online"
#define MQTT_STATUS_OFFLINE "offline"

typedef struct {
    const uint8_t *mqtt_buffer;
    size_t mqtt_buffer_size;
    char data_format[10];
    esp_err_t status;
    Rec_config_file_t *rec_config_file;
} task_params_t_publish_audio_chunks;

bool mqtt_init_client(Rec_config_file_t *rec_config_file);
void publish_audio_chunks(void* pvpublish_audio_chunks);

#endif
