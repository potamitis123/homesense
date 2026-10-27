#include "math.h"
#include "i2s_audio.h"
#include "esp_log.h"
#include "esp_heap_caps.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_timer.h"
#include "esp_system.h"



static const char *TAG_I2S_AUDIO = "I2S_AUDIO";
static i2s_chan_handle_t rx_handle = NULL;
static portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
static void print_free_heap_i2s(void){
    #if MEM_STATISTICS
    ESP_LOGW(TAG_I2S_AUDIO, "Get Core: %d", esp_cpu_get_core_id());
    ESP_LOGW(TAG_I2S_AUDIO, "Get Free heap memory: %ld", esp_get_free_heap_size());
    ESP_LOGW(TAG_I2S_AUDIO, "Get Free heap internal memory: %ld", esp_get_free_internal_heap_size());
    ESP_LOGW(TAG_I2S_AUDIO, "Get Minimum ever free heap memory: %ld", esp_get_minimum_free_heap_size());
    #endif
}



float hp_alpha = 0.96; // fc ≈ fs / (2π) * (1 - α)
int16_t prev_input = 0;
int16_t prev_output = 0;

int16_t highpass_filter(int16_t input) {
  int16_t output = hp_alpha * (prev_output + input - prev_input);
  prev_input = input;
  prev_output = output;
  return output;
}


void process_i2s_data(const int32_t* raw_samples,
                     const size_t samples_count, 
                     int16_t* processed_samples, 
                     double *rms) {
    if (processed_samples == NULL || samples_count == 0) {
        ESP_LOGE(TAG_I2S_AUDIO, "Invalid input parameters");
        return;
    }

    taskENTER_CRITICAL(&mux);
    const int gain =1;  // Consider making this value configurable
    double sum = 0;
    prev_input = 0;
    prev_output = 0;
    for (size_t i = 0; i < samples_count; i++) {
        int32_t raw_value = raw_samples[i] * 2;
        int16_t value_16bit = (int16_t)(raw_value / 65536);//256
        value_16bit = highpass_filter(value_16bit);
       // int16_t value_16bit = (int16_t)(value_24bit >> 8);
        int16_t amplified = value_16bit * gain;
      //  if (amplified > 32767) amplified = 32767;
        //if (amplified < -32768) amplified = -32768;
        processed_samples[i] = (int16_t) amplified ;
        // if (i%1000 == 0){
        //     ESP_LOGI(TAG_I2S_AUDIO, " %ld    %d",raw_samples[i], processed_samples[i]);
        //     vTaskDelay(pdMS_TO_TICKS(10));

        // }
        sum += (double)(processed_samples[i] * processed_samples[i]);
    }
    double mean = (sum / (double)samples_count);
    //ESP_LOGI(TAG_I2S_AUDIO, " mean %f    ",mean);
    *rms = sqrt(mean);
    taskEXIT_CRITICAL(&mux);
    
}
void i2s_init(uint32_t sample_rate, uint8_t sample_bits) {
    ESP_LOGI(TAG_I2S_AUDIO, "Initializing I2S");

//     i2s_config = {
//     .mode = i2s_mode_t(I2S_MODE_MASTER | I2S_MODE_RX),
//     .sample_rate = SAMPLE_RATE,
//     .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,
//     .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
//     .communication_format = i2s_comm_format_t(I2S_COMM_FORMAT_I2S),
//     .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
//     .dma_buf_count = 8,
//     .dma_buf_len = 512,
//     .use_apll = false,
//     .tx_desc_auto_clear = false,
//     .fixed_mclk = 0
//   };


    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    
    ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, NULL, &rx_handle));

    i2s_std_config_t std_cfg = {
        .clk_cfg = {
            
            .sample_rate_hz = sample_rate,
            .clk_src = I2S_CLK_SRC_DEFAULT,
            .mclk_multiple = I2S_MCLK_MULTIPLE_256,
        },
        .slot_cfg = {
            .data_bit_width = sample_bits,
            .slot_bit_width = I2S_SLOT_BIT_WIDTH_32BIT,
            .slot_mode = I2S_SLOT_MODE_MONO,
            .slot_mask = I2S_STD_SLOT_LEFT,
            .ws_width = 32,
            .ws_pol = false,
            .bit_shift = true,
        },
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = I2S_BCK_IO,
            .ws = I2S_WS_IO,
            .dout = I2S_GPIO_UNUSED,
            .din = I2S_DI_IO,
            .invert_flags = {
                .mclk_inv = false,
                .bclk_inv = false,
                .ws_inv = false,
            },
        },
    };

    ESP_ERROR_CHECK(i2s_channel_init_std_mode(rx_handle, &std_cfg));
    ESP_ERROR_CHECK(i2s_channel_enable(rx_handle));
}



static uint16_t* reallocate_buffer(uint16_t* buffer, size_t new_size) {
    uint16_t* new_buffer = (uint16_t*)heap_caps_realloc(buffer, new_size, MALLOC_CAP_SPIRAM);
    if (new_buffer == NULL) {
        ESP_LOGE(TAG_I2S_AUDIO, "Failed to reallocate buffer");
        heap_caps_free(buffer);
        return NULL;
    }
    return new_buffer;
}

void time_message_i2s(int64_t start_time,char *message){
    int64_t end_tick = esp_timer_get_time();
    int64_t duration_ = end_tick - start_time;
    ESP_LOGW(TAG_I2S_AUDIO,"start %lld end  %lld -- %s Time %lld microsecond %lld millisecond",start_time,end_tick,message,duration_,duration_/1000);
}

void task_record_and_encode_audio(void* pv_i2s_audio_params)
{
    task_params_t_i2s_audio* params = (task_params_t_i2s_audio*)pv_i2s_audio_params;
    Rec_config_file_t *rec_config_file = params->rec_config_file;
    RingbufHandle_t ring_buffer_handle = (RingbufHandle_t)params->ring_buffer_handle;

    ESP_LOGI(TAG_I2S_AUDIO, "Starting audio recording");
    ///size_t buffer_size = rec_config_file->sample_rate * rec_config_file->record_time * (SAMPLE_BITS / 8);    
   // uint32_t* raw_buffer = (uint32_t*)heap_caps_malloc(buffer_size, MALLOC_CAP_SPIRAM);
    while(true){
        size_t buffer_size = rec_config_file->sample_rate * rec_config_file->record_time * (SAMPLE_BITS / 8);    
        int32_t* raw_buffer = (int32_t*)heap_caps_malloc(buffer_size, MALLOC_CAP_SPIRAM);
        int64_t start_mic = esp_timer_get_time();
        if(raw_buffer==NULL){
            ESP_LOGE(TAG_I2S_AUDIO, "Failed to allocate raw buffer");
        }
        else{
            //ESP_LOGI(TAG_I2S_AUDIO, "raw_buffer size %d",buffer_size);
            size_t bytes_read=0;
            ESP_ERROR_CHECK(i2s_channel_read(rx_handle, raw_buffer, buffer_size, &bytes_read, portMAX_DELAY));
           // ESP_LOGI(TAG_I2S_AUDIO, "Data size: %d bytes", bytes_read);   
            if (bytes_read > 0) {
            //    ESP_LOGI(TAG_I2S_AUDIO, "Recorded %d bytes", bytes_read);
                size_t samples_count = bytes_read / sizeof(int32_t);
                size_t samples_bytes = samples_count * sizeof(int16_t);
                int16_t* audio_buffer = (int16_t*)heap_caps_malloc(samples_bytes, MALLOC_CAP_SPIRAM);
                //ESP_LOGW(TAG_I2S_AUDIO, "Malloc  audio_buffer %u",*audio_buffer);
                if (audio_buffer != NULL) {  
                    double rms=0;
                    process_i2s_data(raw_buffer, samples_count, audio_buffer, &rms);
                    //ESP_LOGI(TAG_I2S_AUDIO, "Data 16bit size: %d samples, %d bytes", samples_count,samples_bytes);
                    if (rms >= rec_config_file->rms){
                        ESP_LOGW(TAG_I2S_AUDIO, "RMS %f  Threshold RMS %f ", rms,rec_config_file->rms);
                        // BaseType_t ret=xRingbufferSendFromISR(ring_buffer_handle,audio_buffer,samples_bytes,NULL);
                        //ESP_LOGW(TAG_I2S_AUDIO, "before xRingbufferSend  audio_buffer %u",*audio_buffer);
                        BaseType_t ret=xRingbufferSend(ring_buffer_handle,audio_buffer,samples_bytes,pdMS_TO_TICKS(10));
                       // ESP_LOGW(TAG_I2S_AUDIO, "after xRingbufferSend  audio_buffer %u",*audio_buffer);
                        if (ret!=pdPASS){
                            ESP_LOGE(TAG_I2S_AUDIO, "Failed to send item to ring buffer");
                        }
                        else{
                            ESP_LOGI(TAG_I2S_AUDIO, "Send ring buffer %d bytes",samples_bytes);
                        }
                        size_t free_space = xRingbufferGetCurFreeSize(ring_buffer_handle);  
                        ESP_LOGI(TAG_I2S_AUDIO, "***** Ring Buffer free space  %d *****",free_space);
                    }
                    else{
                        ESP_LOGW(TAG_I2S_AUDIO, "RMS too low %f  Threshold RMS %f ", rms,rec_config_file->rms);
                    }
                }
                else{
                    ESP_LOGE(TAG_I2S_AUDIO, "Failed to allocate audio buffer");
                }
                if (audio_buffer != NULL)
                {
                  //  ESP_LOGW(TAG_I2S_AUDIO, "Free audio_buffer %u",*audio_buffer);
                    heap_caps_free(audio_buffer);  
                    audio_buffer=NULL;
                    //ESP_LOGI(TAG_I2S_AUDIO, "Free audio_buffer");
                }
            }
            else{
                ESP_LOGE(TAG_I2S_AUDIO, "Failed to read data from I2S");
            }
           
            
        }
        print_free_heap_i2s();
        vTaskDelay(pdMS_TO_TICKS(200));
        time_message_i2s(start_mic,"i2s_audio");
        if (raw_buffer != NULL){
           heap_caps_free(raw_buffer);
            raw_buffer=NULL;
            //ESP_LOGI(TAG_I2S_AUDIO, "Free raw_buffer");
        }
    
    }
    // if (raw_buffer != NULL){
    //     heap_caps_free(raw_buffer);
    //     raw_buffer=NULL;
    //     //ESP_LOGI(TAG_I2S_AUDIO, "Free raw_buffer");
    // }
    vTaskDelete(NULL);  
}