#include "my_mqtt_client.h"
#include "esp_log.h"

static const char *TAG = "MQTT_CLIENT";
static esp_mqtt_client_handle_t mqtt_client = NULL;
static EventGroupHandle_t mqtt_event_group=NULL;
static char mqtt_status_topic[100] = {0};

static void publish_status(const char *status, bool retain){
    if (mqtt_client == NULL || mqtt_status_topic[0] == '\0') {
        return;
    }
    esp_mqtt_client_publish(mqtt_client, mqtt_status_topic, status, strlen(status), 1, retain);
}
#if MQTT_TLS
extern const uint8_t client_cert_pem_start[] asm("_binary_hlias_crt_start");
extern const uint8_t client_cert_pem_end[] asm("_binary_hlias_crt_end");
extern const uint8_t client_key_pem_start[] asm("_binary_hlias_key_start");
extern const uint8_t client_key_pem_end[] asm("_binary_hlias_key_end");
extern const uint8_t server_cert_pem_start[] asm("_binary_ca_crt_start");
extern const uint8_t server_cert_pem_end[] asm("_binary_ca_crt_end");
#endif
static void saveconfig_and_restart(char * config_str,int config_len){
    Rec_config_file_t *rec_config_file=malloc(sizeof(Rec_config_file_t));
    char config_json_string[config_len+1];
    snprintf(config_json_string, sizeof(config_json_string), "%.*s",config_len+1,config_str);
    read_config_string(config_json_string,rec_config_file);
    print_config_file(rec_config_file);
    save_config_nvs(rec_config_file);
    publish_status(MQTT_STATUS_OFFLINE, 1);
    vTaskDelay(pdMS_TO_TICKS(200));
    ESP_LOGE(TAG, "restart");
    esp_restart();
}
static void mqtt_event_handler(void *handler_args, esp_event_base_t base, 
                             int32_t event_id, void *event_data) {
   ESP_LOGD(TAG, "Event dispatched from event loop base=%s, event_id=%" PRIi32 "", base, event_id);
    esp_mqtt_event_handle_t event = event_data;
    switch ((esp_mqtt_event_id_t)event_id) {
    case MQTT_EVENT_CONNECTED:
        ESP_LOGI(TAG, "MQTT_EVENT_CONNECTED");
        xEventGroupSetBits(mqtt_event_group, MQTT_CONNECTED_BIT);
        break;
    case MQTT_EVENT_DISCONNECTED:
        ESP_LOGI(TAG, "MQTT_EVENT_DISCONNECTED");
        vTaskDelay(pdMS_TO_TICKS(200));
        ESP_LOGE(TAG, "restart");
        esp_restart();
        break;
    case MQTT_EVENT_PUBLISHED:
        ESP_LOGI(TAG, "MQTT_EVENT_PUBLISHED, msg_id=%d", event->msg_id);
        break;
    case MQTT_EVENT_ERROR:
        ESP_LOGI(TAG, "MQTT_EVENT_ERROR");
        if (event->error_handle->error_type == MQTT_ERROR_TYPE_TCP_TRANSPORT) {
            ESP_LOGI(TAG, "Last error code reported from esp-tls: 0x%x", event->error_handle->esp_tls_last_esp_err);
            ESP_LOGI(TAG, "Last tls stack error number: 0x%x", event->error_handle->esp_tls_stack_err);
            ESP_LOGI(TAG, "Last captured errno : %d (%s)",  event->error_handle->esp_transport_sock_errno,
                     strerror(event->error_handle->esp_transport_sock_errno));
        }
        vTaskDelay(pdMS_TO_TICKS(200));
        ESP_LOGE(TAG, "restart");
        esp_restart();
        break;
    case MQTT_EVENT_SUBSCRIBED:
        ESP_LOGW(TAG, "MQTT_EVENT_SUBSCRIBED, msg_id=%d", event->msg_id);
        break;
    case MQTT_EVENT_UNSUBSCRIBED:
        ESP_LOGW(TAG, "MQTT_EVENT_UNSUBSCRIBED, msg_id=%d", event->msg_id);
        break;
    case MQTT_EVENT_DATA:
        ESP_LOGI(TAG, "MQTT_EVENT_DATA, msg_id=%d", event->msg_id);
        ESP_LOGI(TAG, "Topic: %.*s", event->topic_len, event->topic);
        ESP_LOGI(TAG, "Data: %.*s", event->data_len, event->data);
        esp_mqtt_client_publish(mqtt_client,event->topic,"",0,0,1);
        saveconfig_and_restart(event->data,event->data_len);
        break;

    default:
        ESP_LOGI(TAG, "Other event id:%d", event->event_id);
        break;
    }
}

bool mqtt_init_client(Rec_config_file_t *rec_config_file) {
    mqtt_event_group = xEventGroupCreate();
    xEventGroupSetBits(mqtt_event_group, MQTT_NOT_CONNECTED_BIT);

    snprintf(mqtt_status_topic, sizeof(mqtt_status_topic), "%s/%s", MQTT_STATUS_TOPIC, rec_config_file->mqtt_client_id);

#if MQTT_TLS
    esp_mqtt_client_config_t mqtt_cfg = {
        .broker.address.hostname = rec_config_file->mqtt_broker,
        .broker.address.port = rec_config_file->mqtt_port,
        .broker.address.transport = MQTT_TRANSPORT_OVER_SSL,
        .credentials.client_id = rec_config_file->mqtt_client_id,
        .credentials.username = rec_config_file->mqtt_client_id,
        .credentials.authentication.password = rec_config_file->mqtt_password,
        .broker.verification.certificate = (const char *)server_cert_pem_start,
        .credentials.authentication.certificate = (const char *)client_cert_pem_start,
        .credentials.authentication.key = (const char *)client_key_pem_start,
        .session.last_will.topic = mqtt_status_topic,
        .session.last_will.msg = MQTT_STATUS_OFFLINE,
        .session.last_will.qos = 1,
        .session.last_will.retain = 1,
    };
#else
   esp_mqtt_client_config_t mqtt_cfg = {
       .broker.address.hostname = rec_config_file->mqtt_broker,
        .broker.address.port = rec_config_file->mqtt_port,
        
        .broker.address.transport = MQTT_TRANSPORT_OVER_TCP,
        .credentials.client_id = rec_config_file->mqtt_client_id,
        .credentials.username = rec_config_file->mqtt_client_id,
        .credentials.authentication.password = rec_config_file->mqtt_password,
        .session.last_will.topic = mqtt_status_topic,
        .session.last_will.msg = MQTT_STATUS_OFFLINE,
        .session.last_will.qos = 1,
        .session.last_will.retain = 1,
    };
#endif

    mqtt_client = esp_mqtt_client_init(&mqtt_cfg);
    if (mqtt_client == NULL) {
        ESP_LOGE(TAG, "Failed to initialize MQTT client");
        return false;
    }

    ESP_ERROR_CHECK(esp_mqtt_client_register_event(mqtt_client, ESP_EVENT_ANY_ID, mqtt_event_handler, NULL));
    ESP_ERROR_CHECK(esp_mqtt_client_start(mqtt_client));

    // Περιμένουμε για τη σύνδεση MQTT
    EventBits_t bits = xEventGroupWaitBits(mqtt_event_group,
            MQTT_CONNECTED_BIT,
            pdFALSE,
            pdFALSE,
            pdMS_TO_TICKS(10000));  // 10 second timeout
    
     vTaskDelay(pdMS_TO_TICKS(200));// 
    
    if (bits & MQTT_CONNECTED_BIT) {
        ESP_LOGI(TAG, "Connected to MQTT broker");
        char conf_topic[100];
        snprintf(conf_topic, sizeof(conf_topic), "%s/%s",  "/audio/config",rec_config_file->mqtt_client_id);
        esp_mqtt_client_subscribe(mqtt_client, conf_topic, 1);

        ESP_LOGW(TAG,"sub topic %s",conf_topic);

        publish_status(MQTT_STATUS_ONLINE, 1);
        ESP_LOGW(TAG, "status topic %s -> online (LWT set to offline)", mqtt_status_topic);

        return true;
    } else {
        ESP_LOGE(TAG, "Failed to connect to MQTT broker");
        return false;
    }
}

void publish_audio_chunks(void* pvpublish_audio_chunks) {

    task_params_t_publish_audio_chunks* params = (task_params_t_publish_audio_chunks*)pvpublish_audio_chunks;

    ESP_LOGI(TAG, "Starting chunked publishing, total size: %d", params->mqtt_buffer_size);

    uint64_t mqtt_delay=60;//

    int chunks = (params->mqtt_buffer_size + CHUNK_SIZE - 1) / CHUNK_SIZE;
    int success_count = 0;

    // Create info message

    char topic[100];
    snprintf(topic, sizeof(topic), "%s/%s/info",  MQTT_TOPIC,params->rec_config_file->mqtt_client_id);
    char info[250];
    snprintf(info, sizeof(info), "{\"total_size\":%d,\"duration\":%d,\"format\":\"%s\",\"chunks\":%d,\"samplerate\":%d,\"bits\":%d,\"username\":\"%s\",\"rms_threshold\":%lf,\"esp_audio_duration\":%d}", 
                                params->mqtt_buffer_size, 
                                params->rec_config_file->record_time, 
                                params->data_format, 
                                chunks, 
                                params->rec_config_file->sample_rate, 
                                params->rec_config_file->sample_bits,
                                params->rec_config_file->mqtt_client_id,
                                params->rec_config_file->rms,
                                params->rec_config_file->record_time);
    int message_id=esp_mqtt_client_publish(mqtt_client,
                                          topic, 
                                          info, 
                                          strlen(info), 
                                          1, 
                                          0);
    ESP_LOGI(TAG, "%s",info);
    
    // Split and send each chunk
    for (int i = 0; i < chunks; i++) {
        size_t chunk_start = i * CHUNK_SIZE;
        size_t chunk_size = ((chunk_start + CHUNK_SIZE) > params->mqtt_buffer_size) ? 
                            (params->mqtt_buffer_size - chunk_start) : CHUNK_SIZE;
        
        // Create topic for each chunk
        snprintf(topic, sizeof(topic), "%s/%s/chunk/%d", MQTT_TOPIC,params->rec_config_file->mqtt_client_id, i);
        
        // Send the chunk
        int msg_id = esp_mqtt_client_publish(mqtt_client, 
                                            topic,
                                            (const char*)(params->mqtt_buffer + chunk_start),
                                            chunk_size,
                                            0, // 0 = QoS 0 (At most once delivery) 1=QoS 1 (At least once delivery) 2=QoS 2 (Exactly once delivery)
                                            0);// 0 = retain message (Not retained)
        
        if (msg_id != -1) {
            success_count++;
            ESP_LOGI(TAG, "Published chunk %d/%d, size: %d bytes", i+1, chunks, chunk_size);
        } else {
            ESP_LOGE(TAG, "Failed to publish chunk %d/%d", i+1, chunks);
            params->status = ESP_FAIL;
            break;
        }
        // Delay between chunks 
        vTaskDelay(pdMS_TO_TICKS(mqtt_delay));// 
    }
    // Create complete message
    snprintf(topic, sizeof(topic), "%s/%s/complete", MQTT_TOPIC,params->rec_config_file->mqtt_client_id);
    char complete_info[100];
    snprintf(complete_info, sizeof(complete_info), 
             "{\"successful_chunks\":%d,\"total_chunks\":%d}", 
             success_count, chunks);
    esp_mqtt_client_publish(mqtt_client, 
                            topic, 
                            complete_info, 
                            strlen(complete_info), 
                            1, 
                            0);
    ESP_LOGI(TAG,"{\"Send Number of successful chunks\":%d,\" from Total chunks\":%d}",success_count, chunks);
    
    //vTaskDelete(NULL);
}