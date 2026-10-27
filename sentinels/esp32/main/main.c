#include <stdio.h>
#include <string.h>
#include "global_heads.h"
#include "esp_system.h"
#include "freertos/ringbuf.h"
#include "nvs_flash.h"
#include "esp_log.h"

#include "wifi_manager.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_system.h"
#include "confing_file.h"

Rec_config_file_t config_file;

#if MQTT_SEND
    #include "my_mqtt_client.h"
#endif

#if ENCODER_TYPE == MP3
    #include "esp_lame.h"
#endif
#if MIC_TYPE == I2S
    #include "i2s_audio.h"
#elif MIC_TYPE == USB
    #include "driver/usb_serial_jtag.h"
    #include "usb_audio.h"
    void ensure_usb_phy_free(void) {
    // Απενεργοποίησε οποιοδήποτε USB Serial/JTAG interface
    #if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG || CONFIG_USJ_ENABLE_USB_SERIAL_JTAG
        usb_serial_jtag_driver_uninstall();
    #endif
    
    // Μικρή καθυστέρηση για να ελευθερωθεί το PHY
    vTaskDelay(pdMS_TO_TICKS(100));
}
#endif
static const char *TAG = "MAIN";

void time_message(int64_t start_time,char *message){
    int64_t end_tick = esp_timer_get_time();
    int64_t duration_ = end_tick - start_time;
    ESP_LOGI(TAG,"%s Time %lld microsecond %lld millisecond",message,duration_,duration_/1000);
}

static void print_free_heap(void){
    #if MEM_STATISTICS
    ESP_LOGW(TAG, "Get Core: %d", esp_cpu_get_core_id());
    ESP_LOGW(TAG, "Get Free heap memory: %ld", esp_get_free_heap_size());
    ESP_LOGW(TAG, "Get Free heap internal memory: %ld", esp_get_free_internal_heap_size());
    ESP_LOGW(TAG, "Get Minimum ever free heap memory: %ld", esp_get_minimum_free_heap_size());
    #endif
}

static wav_header_t generate_wav_header(uint32_t sample_rate, uint16_t bit_depth, uint16_t num_channels, uint32_t data_size) {
    wav_header_t header;
    memcpy(header.riff_header, "RIFF", 4);
    header.wav_size = data_size + 36;
    memcpy(header.wave_header, "WAVE", 4);
    memcpy(header.fmt_header, "fmt ", 4);
    header.fmt_chunk_size = 16;
    header.audio_format = 1; // PCM
    header.num_channels = num_channels;
    header.sample_rate = sample_rate;
    header.byte_rate = sample_rate * num_channels * (bit_depth / 8);
    header.sample_alignment = num_channels * (bit_depth / 8);
    header.bit_depth = bit_depth;
    memcpy(header.data_header, "data", 4);
    header.data_size = data_size;
    return header;
}

typedef struct {
    RingbufHandle_t ring_buffer_handle;
    size_t max_item_size;
    esp_err_t status;
    Rec_config_file_t *rec_config_file;
    TaskHandle_t task_handle;
} task_convert_and_send_params;

void task_convert_and_send_audio(void* pv_convert_and_send_params){
    //Get the parameters
    task_convert_and_send_params* params = (task_convert_and_send_params*)pv_convert_and_send_params;
    Rec_config_file_t *rec_config_file = params->rec_config_file;
    RingbufHandle_t ring_buffer_handle = (RingbufHandle_t)params->ring_buffer_handle;
    size_t max_buffer_size = params->max_item_size;
#if ENCODER_TYPE == MP3    
    // lame handler
    esp_lame_encoder_t *encoder_handle=NULL;
#endif
    int duration = rec_config_file->record_time * 1000;

    // ΠΡΟΣΘΗΚΗ: Counter για διαδοχικά σφάλματα
    int consecutive_failures = 0;
    const int MAX_CONSECUTIVE_FAILURES = 10;

    while(true){
        uint8_t *pcm_buffer = NULL;
#if ENCODER_TYPE == MP3        
        lame_encode_params_t *params_encoder_task=NULL;
#endif
        int64_t start_loop = esp_timer_get_time();
        
        // Get audio data from RingBuffer με μεγαλύτερο timeout
        size_t pcm_buffer_size;
        if (pcm_buffer == NULL) {
            pcm_buffer = (uint8_t*)xRingbufferReceiveUpTo(ring_buffer_handle,
                                                          &pcm_buffer_size,
                                                          pdMS_TO_TICKS(duration + 5000), // +5 seconds extra
                                                          max_buffer_size
                                                          );
            
            if(pcm_buffer == NULL){
                consecutive_failures++;
                ESP_LOGW(TAG, "No data available in ring buffer (%d/%d)", consecutive_failures, MAX_CONSECUTIVE_FAILURES);
                
                // ΒΕΛΤΙΩΣΗ: Έλεγχος για USB σύνδεση
#if MIC_TYPE == USB
                if (!usb_audio_is_connected()) {
                    ESP_LOGW(TAG, "USB device not connected, waiting...");
                    vTaskDelay(pdMS_TO_TICKS(2000));
                    consecutive_failures = 0; // Reset counter when USB issue detected
                    continue;
                }
                
                if (!usb_audio_is_streaming()) {
                    ESP_LOGW(TAG, "USB streaming not active, waiting...");
                    vTaskDelay(pdMS_TO_TICKS(1000));
                    continue;
                }
#endif
                
                if (consecutive_failures >= MAX_CONSECUTIVE_FAILURES) {
                    ESP_LOGE(TAG, "Too many consecutive failures, something is wrong");
                    // Μπορείς να προσθέσεις restart logic εδώ αν χρειάζεται
                    consecutive_failures = 0;
                }
                
                // Μικρότερο delay για γρηγορότερη ανάκαμψη
                vTaskDelay(pdMS_TO_TICKS(500));
                continue;
            }
            else{
                consecutive_failures = 0; // Reset counter on success
                ESP_LOGI(TAG,"pcm_buffer_size %d",pcm_buffer_size);
            }
        }
        
        if(pcm_buffer != NULL){
            
#if ENCODER_TYPE == MP3
            //Init lame encoder
            if(encoder_handle == NULL){
                int64_t start_init_lame = esp_timer_get_time();
                ESP_LOGI(TAG, "Start Init LAME");
                encoder_handle = (esp_lame_encoder_t*)heap_caps_malloc(sizeof(esp_lame_encoder_t), MALLOC_CAP_SPIRAM);
                if (encoder_handle == NULL) {
                    ESP_LOGE(TAG, "Failed to allocate lame encoder in PSRAM");
                    // Return the buffer before restarting
                    vRingbufferReturnItem(ring_buffer_handle, (void*)pcm_buffer);
                    vTaskDelay(pdMS_TO_TICKS(1000));
                    esp_restart();
                }else{
                    esp_lame_config_t config = {
                        .sample_rate = 16000,
                        .channels = 1,
                        .bitrate = 64000,
                        .quality = 0
                    };
            
                    esp_err_t ret = esp_lame_init(&config, encoder_handle);
                    if (ret != ESP_OK) {
                        ESP_LOGE(TAG, "Failed to initialize LAME encoder");
                        // Cleanup before restart
                        heap_caps_free(encoder_handle);
                        encoder_handle = NULL;
                        vRingbufferReturnItem(ring_buffer_handle, (void*)pcm_buffer);
                        vTaskDelay(pdMS_TO_TICKS(1000));
                        esp_restart();
                    }
                }
                ESP_LOGI(TAG, "End Init MP3");
                time_message(start_init_lame,"Init Lame");
            }
            
            //Start lame encoder
            int64_t start_encode_lame = esp_timer_get_time();
            size_t mp3_size = (size_t)(1.25 * pcm_buffer_size) + 7200;
            params_encoder_task = (lame_encode_params_t *)heap_caps_malloc(sizeof(lame_encode_params_t), MALLOC_CAP_SPIRAM);
            
            if (params_encoder_task == NULL) {
                ESP_LOGE(TAG, "Failed to allocate encoder params");
                vRingbufferReturnItem(ring_buffer_handle, (void*)pcm_buffer);
                vTaskDelay(pdMS_TO_TICKS(100));
                continue;
            }
            
            params_encoder_task->encoder_handle = *encoder_handle;
            params_encoder_task->pcm_buffer = (int16_t*)pcm_buffer;
            params_encoder_task->pcm_size = pcm_buffer_size;
            params_encoder_task->encoded_buffer = NULL;
            params_encoder_task->encoded_size = &mp3_size;
            params_encoder_task->encode_event_group = NULL;
            params_encoder_task->status = ESP_OK;
            params_encoder_task->encoded_buffer = (uint8_t*)heap_caps_malloc(mp3_size, MALLOC_CAP_SPIRAM);
            
            if (params_encoder_task->encoded_buffer == NULL) {
                ESP_LOGE(TAG, "Failed to allocate encoded buffer");
                heap_caps_free(params_encoder_task);
                vRingbufferReturnItem(ring_buffer_handle, (void*)pcm_buffer);
                vTaskDelay(pdMS_TO_TICKS(100));
                continue;
            }
            
            lame_encode_task(params_encoder_task);
            esp_err_t rec_status = params_encoder_task->status;
            size_t encoded_buffer_size = *params_encoder_task->encoded_size;
            time_message(start_encode_lame,"Lame encode time");
            print_free_heap();

#elif ENCODER_TYPE == RAW
            esp_err_t rec_status = ESP_OK;
            size_t encoded_buffer_size = pcm_buffer_size;
            ESP_LOGI(TAG, "Free heap: %ld", esp_get_free_heap_size());
#elif ENCODER_TYPE == WAV
            esp_err_t rec_status = ESP_OK;
            size_t wav_header_size = sizeof(wav_header_t);
            size_t encoded_buffer_size = wav_header_size + pcm_buffer_size;
            
            uint8_t *wav_buffer = (uint8_t*)heap_caps_malloc(encoded_buffer_size, MALLOC_CAP_SPIRAM);
            if (wav_buffer == NULL) {
                ESP_LOGE(TAG, "Failed to allocate WAV buffer");
                rec_status = ESP_FAIL;
            } else {
                wav_header_t header = generate_wav_header(
                    rec_config_file->sample_rate,
                    rec_config_file->sample_bits,
                    1, // Mono
                    pcm_buffer_size
                );
                memcpy(wav_buffer, &header, wav_header_size);
                memcpy(wav_buffer + wav_header_size, pcm_buffer, pcm_buffer_size);
            }
#endif

#if MQTT_SEND
            if (rec_status == ESP_OK) {
                if (encoded_buffer_size > 0) {
                    ESP_LOGI(TAG, "Successfully pcm data size: %d bytes, encoded data, size: %d bytes",pcm_buffer_size, encoded_buffer_size);

#if ENCODER_TYPE == MP3
                    task_params_t_publish_audio_chunks params = {
                        .mqtt_buffer = params_encoder_task->encoded_buffer,
                        .mqtt_buffer_size = encoded_buffer_size,
                        .status = ESP_OK,
                        .data_format = "mp3",
                        .rec_config_file = &config_file
                    };
#elif ENCODER_TYPE == RAW
                    task_params_t_publish_audio_chunks params = {
                        .mqtt_buffer = pcm_buffer,
                        .mqtt_buffer_size = encoded_buffer_size,
                        .status = ESP_OK,
                        .data_format = "raw",
                        .rec_config_file = &config_file
                    };
#elif ENCODER_TYPE == WAV
                    task_params_t_publish_audio_chunks params = {
                        .mqtt_buffer = wav_buffer,
                        .mqtt_buffer_size = encoded_buffer_size,
                        .status = ESP_OK,
                        .data_format = "wav",
                        .rec_config_file = &config_file
                    };
#endif  
                    ESP_LOGI(TAG, "Start MQTT");
                    int64_t start_mqtt = esp_timer_get_time();
                    publish_audio_chunks(&params);
                    time_message(start_mqtt,"Send mqtt");
                }
                print_free_heap();
            }
#endif

#if ENCODER_TYPE == MP3
            // Cleanup encoder resources
            if(params_encoder_task != NULL) {
                if(params_encoder_task->encoded_buffer != NULL){
                    heap_caps_free(params_encoder_task->encoded_buffer);
                    params_encoder_task->encoded_buffer = NULL;
                    ESP_LOGI(TAG, "Free params_encoder_task->encoded_buffer memory");
                }
                heap_caps_free(params_encoder_task);
                params_encoder_task = NULL;
                ESP_LOGI(TAG, "Free params_encoder_task memory");
            }
#elif ENCODER_TYPE == RAW
           
            ESP_LOGI(TAG, "Nothing ");
#elif ENCODER_TYPE == WAV
            if (wav_buffer != NULL) {
                heap_caps_free(wav_buffer);
                wav_buffer = NULL;
                ESP_LOGI(TAG, "Free WAV buffer memory");
            }
#endif
            
            // Return buffer to ring buffer
            vRingbufferReturnItem(ring_buffer_handle, (void*)pcm_buffer);
            pcm_buffer = NULL;
            ESP_LOGI(TAG, "Returned pcm_buffer to ring buffer");
        }
        
        print_free_heap();
        time_message(start_loop,"Total Encode Send Loop");
        vTaskDelay(pdMS_TO_TICKS(100)); // Μικρότερο delay για καλύτερη απόκριση
    }
    
#if ENCODER_TYPE == MP3
    if (encoder_handle != NULL) {
        esp_lame_cleanup(&encoder_handle);
    }
#endif
}
void maxloop_delay(int duration){
    int64_t start_delay = esp_timer_get_time();
    while ( (esp_timer_get_time() - start_delay) < (duration * 1000) ){
        ESP_LOGI(TAG, "Waiting to complete max loop delay...");
    }
    time_message(start_delay,"Max Loop Delay");
}

void task_reset_every_day(void *pvParameters) {
    const TickType_t one_hour = pdMS_TO_TICKS(60UL * 60UL * 1000UL);
    const uint8_t hours_per_day = 24;

    while (1) {
        ESP_LOGI(TAG, "Waiting for 24 hours before reset...");
        
        // Περίμενε 24 φορές x 1 ώρα
        for (uint8_t i = 0; i < hours_per_day; i++) {
            vTaskDelay(one_hour);
        }

        ESP_LOGE(TAG, "24 ώρες πέρασαν, κάνω esp_restart()");
    
        esp_restart();
    }
}
void create_daily_reset_task() {
    BaseType_t result = xTaskCreate(&task_reset_every_day, "DailyResetTask", 2048, NULL, 1, NULL);
    if (result != pdPASS) {
        ESP_LOGE(TAG, "Failed to create daily reset task");
    }
}

void app_main(void) {
    esp_err_t ret;
    create_daily_reset_task();
    

    // Initialize NVS
    ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
    print_free_heap();

    #if  FIRSTBOOT == 0
        ret =read_config_nvs(&config_file);
        if(ret!=ESP_OK){
            ESP_LOGE(TAG, "Failed to read Config");
            write_default_values_to_rec_config_file(&config_file);
            save_config_nvs(&config_file);
        }
        else{
            ESP_LOGI(TAG, "Config readed from NVS");
            if (strcmp(config_file.wifi_ssid, WIFI_SSID) != 0 || strcmp(config_file.wifi_password, WIFI_PASS) != 0) {
                ESP_LOGW(TAG, "WiFi config in NVS differs from global_heads.h. Updating NVS config.");
                strcpy(config_file.wifi_ssid, WIFI_SSID);
                strcpy(config_file.wifi_password, WIFI_PASS);
                save_config_nvs(&config_file);
            }
        }
    #endif
    print_config_file(&config_file);

    
    // Initialize systems
    if(wifi_manager_init()!=ESP_OK){
        ESP_LOGE(TAG, "Failed to init to WiFi");
        return;
    }
    if (!wifi_connect(&config_file)) {
        ESP_LOGE(TAG, "Failed to connect to WiFi");
         esp_restart();
        return;
    }
    print_free_heap();

#if MQTT_SEND
    // Initialize MQTT Client
    if (!mqtt_init_client(&config_file)) {
        ESP_LOGE(TAG, "Failed to connect to MQTT");
        return;
    }
    print_free_heap();
#endif

#if MIC_TYPE == I2S
    // Initialize I2S   
    i2s_init(config_file.sample_rate,SAMPLE_BITS);
#elif MIC_TYPE == USB
    ensure_usb_phy_free();
    usb_audio_init(config_file.record_time,config_file.sample_bits);
#endif
   
   
    print_free_heap();

    //maxloop_delay(40000);
// *** Create Ring Buffer ***
   
    StaticRingbuffer_t *ring_buffer_struct = (StaticRingbuffer_t *)heap_caps_malloc(sizeof(StaticRingbuffer_t),MALLOC_CAP_SPIRAM);
    size_t buffer_size = (config_file.sample_rate * config_file.record_time * (config_file.sample_bits / 8))*10;
    uint8_t *ring_buffer_storage = heap_caps_malloc(buffer_size, MALLOC_CAP_SPIRAM);
    RingbufHandle_t ring_buffer_handle = xRingbufferCreateStatic(buffer_size,RINGBUF_TYPE_BYTEBUF,ring_buffer_storage,ring_buffer_struct);
    if (ring_buffer_handle == NULL) {
        ESP_LOGE(TAG, "Failed to allocate Ring buffer in PSRAM");
        vTaskDelay(pdMS_TO_TICKS(1000));
        esp_restart();
    }
    else{  
        ESP_LOGI(TAG, "Ring Buffer size: %d", (buffer_size*7));
        print_free_heap();
    }
// **********************
// *** Create Task Record Audio ***
#if MIC_TYPE == I2S

    task_params_t_i2s_audio* task_params_i2s_audio=(task_params_t_i2s_audio*)heap_caps_malloc(sizeof(task_params_t_i2s_audio),MALLOC_CAP_SPIRAM);
    //task_params_i2s_audio->task_handle = (TaskHandle_t *)malloc(sizeof(TaskHandle_t));
    task_params_i2s_audio->ring_buffer_handle = ring_buffer_handle;
    task_params_i2s_audio->max_item_size=buffer_size;
    task_params_i2s_audio->rec_config_file=&config_file;
    TaskHandle_t task_handle_i2s_audio ;

    BaseType_t task_ret=xTaskCreatePinnedToCore(&task_record_and_encode_audio,
                                                "i2s_audio",
                                                1024*4,
                                                task_params_i2s_audio,
                                                5,
                                                &task_handle_i2s_audio,
                                                0);
#elif MIC_TYPE == USB    
    task_params_t_usb_audio* task_params_usb_audio=(task_params_t_usb_audio*)heap_caps_malloc(sizeof(task_params_t_usb_audio),MALLOC_CAP_SPIRAM);
    task_params_usb_audio->ring_buffer_handle = ring_buffer_handle;
    task_params_usb_audio->max_item_size=buffer_size;
    task_params_usb_audio->rec_config_file=&config_file;
    TaskHandle_t task_handle_usb_audio ;

    BaseType_t task_ret=xTaskCreatePinnedToCore(&task_record_and_encode_audio_usb,
                                                "usb_audio",
                                                1024*4,
                                                task_params_usb_audio,
                                                5,
                                                &task_handle_usb_audio,
                                                0);
#endif
    if (task_ret!=pdPASS){
        ESP_LOGE(TAG, "Failed to create task_record_and_encode_audio");
        vTaskDelay(pdMS_TO_TICKS(100));
        esp_restart();
    }
// **********************

    StaticTask_t xTaskBuffer_convert_send;
    size_t stacksize=1024*40;
    StackType_t *xStack_convert_send=heap_caps_malloc(stacksize, MALLOC_CAP_SPIRAM);
    task_convert_and_send_params* task_convert_and_send=(task_convert_and_send_params*)heap_caps_malloc(sizeof(task_convert_and_send_params),MALLOC_CAP_SPIRAM);
    task_convert_and_send->ring_buffer_handle = ring_buffer_handle ;
    task_convert_and_send->max_item_size=buffer_size;
    task_convert_and_send->rec_config_file=&config_file;
    
    
    
    
    TaskHandle_t xHandle_convert_send =xTaskCreateStaticPinnedToCore(&task_convert_and_send_audio,
                                                    "convert_sent",
                                                    stacksize,
                                                    task_convert_and_send,
                                                    10,
                                                    xStack_convert_send,
                                                    &xTaskBuffer_convert_send,
                                                    1);   
    if (xHandle_convert_send==NULL){
         ESP_LOGE(TAG, "Failed to create task_convert_and_send_audio");
         vTaskDelay(pdMS_TO_TICKS(1000));
         esp_restart();
    }
    
   
   while(true){
    vTaskDelay(pdMS_TO_TICKS(2000));
   }
   ESP_LOGI(TAG, "End");
}