#include "usb_audio.h"
#define MIC_VOLUME 90
#define NORMALIZE 1

static const char *TAG_USB_AUDIO = "USB_AUDIO";
static portMUX_TYPE mux_usb = portMUX_INITIALIZER_UNLOCKED;

float hp_alpha_usb = 0.96; // fc ≈ fs / (2π) * (1 - α)
int16_t prev_input_usb = 0;
int16_t prev_output_usb = 0;

// USB state variables
static bool usb_device_connected = false;
static bool usb_streaming_active = false;

// ===== CALLBACK APPROACH VARIABLES =====
static int16_t* accumulation_buffer = NULL;
static size_t accumulation_buffer_size = 0;
static size_t current_accumulation_bytes = 0;
static size_t target_accumulation_bytes = 0;
static bool buffer_ready_for_processing = false;
static RingbufHandle_t global_ring_buffer = NULL;
static Rec_config_file_t* global_rec_config = NULL;

// Debug και statistics
static int64_t last_callback_time = 0;
static size_t total_frames_received = 0;

static void print_free_heap_usb(void){
    #if MEM_STATISTICS
    ESP_LOGW(TAG_USB_AUDIO, "Get Core: %d", esp_cpu_get_core_id());
    ESP_LOGW(TAG_USB_AUDIO, "Get Free heap memory: %ld", esp_get_free_heap_size());
    ESP_LOGW(TAG_USB_AUDIO, "Get Free heap internal memory: %ld", esp_get_free_internal_heap_size());
    ESP_LOGW(TAG_USB_AUDIO, "Get Minimum ever free heap memory: %ld", esp_get_minimum_free_heap_size());
    #endif
}

// High-pass filter (ακριβώς όπως στο I2S)
int16_t highpass_filter_usb(int16_t input) {
    int16_t output = hp_alpha_usb * (prev_output_usb + input - prev_input_usb);
    prev_input_usb = input;
    prev_output_usb = output;
    return output;
}

void time_message_usb(int64_t start_time, char *message){
    int64_t end_tick = esp_timer_get_time();
    int64_t duration_ = end_tick - start_time;
    ESP_LOGW(TAG_USB_AUDIO,"start %lld end  %lld -- %s Time %lld microsecond %lld millisecond",start_time,end_tick,message,duration_,duration_/1000);
}

// ===== PROCESS USB AUDIO DATA - ΔΙΟΡΘΩΜΕΝΗ ΕΚΔΟΣΗ =====
size_t process_usb_audio_data_with_downsampling(const int16_t* raw_samples_48k,
                                               const size_t samples_count_48k, 
                                               int16_t* processed_samples_16k, 
                                               double *rms) {
    if (processed_samples_16k == NULL || samples_count_48k == 0) {
        return 0;
    }

    taskENTER_CRITICAL(&mux_usb);
    
    
    const int gain = 1;
    double sum = 0;
    double sum2 = 0;
    prev_input_usb = 0;
    prev_output_usb = 0;
    size_t output_count = 0;
    
    // ===== ΒΗΜΑ 1: ΥΠΟΛΟΓΙΣΜΟΣ DC OFFSET =====
#if NORMALIZE==1  
    int64_t sum_for_dc = 0;
    for (size_t i = 0; i < samples_count_48k; i++) {
        sum_for_dc += (int64_t)raw_samples_48k[i];
    }
    int32_t dc_offset = (int32_t)(sum_for_dc / (int64_t)samples_count_48k);
#else
    int32_t dc_offset = 0;
#endif
    //dc_offset = 0;

    // ===== ΒΗΜΑ 2: DOWNSAMPLING ΜΕ DC OFFSET REMOVAL =====
    // Temporary storage για τα DC-corrected samples
    int16_t* temp_samples = (int16_t*)heap_caps_malloc(samples_count_48k / 3 * sizeof(int16_t), MALLOC_CAP_SPIRAM);
    if (temp_samples == NULL) {
        ESP_LOGE(TAG_USB_AUDIO, "Failed to allocate temp samples buffer");
        taskEXIT_CRITICAL(&mux_usb);
        return 0;
    }
    
    size_t temp_count = 0;
    int32_t max_abs = 0;  // Για normalization
    
    for (size_t i = 0; i < samples_count_48k; i += 3) {
        if (i + 2 < samples_count_48k) {
            // Αφαίρεση DC offset και μέσος όρος 3 samples για anti-aliasing
            int32_t sample1 = (int32_t)raw_samples_48k[i] - dc_offset;
            int32_t sample2 = (int32_t)raw_samples_48k[i + 1] - dc_offset;
            int32_t sample3 = (int32_t)raw_samples_48k[i + 2] - dc_offset;
            
            int32_t sample_sum = sample1 + sample2 + sample3;
            int16_t value_16bit = (int16_t)(sample_sum / 3);
            
            temp_samples[temp_count] = value_16bit;
            
            // Track maximum absolute value για normalization
            int32_t abs_val = abs(value_16bit);
            if (abs_val > max_abs) {
                max_abs = abs_val;
            }
            
            temp_count++;
        }
    }
    
    // ===== ΒΗΜΑ 3: NORMALIZATION/AMPLIFICATION =====
    float normalize_gain = 1.0f;
    
    if (max_abs > 0) {
        // Υπολογισμός gain για να αξιοποιήσουμε το 80% του dynamic range
        float target_max = 32767.0f * 0.8f;  // 80% του max για να αποφύγουμε clipping
        normalize_gain = target_max / (float)max_abs;
        
        // Περιορισμός του gain σε λογικά όρια (1x έως 50x)
        if (normalize_gain > 50.0f) normalize_gain = 50.0f;
        if (normalize_gain < 1.0f) normalize_gain = 1.0f;
    }

    // ===== ΒΗΜΑ 4: FINAL PROCESSING ΜΕ NORMALIZATION =====
    for (size_t i = 0; i < temp_count; i++) {
        int16_t value_16bit = temp_samples[i];
        
        // Apply normalization gain
        int32_t normalized = (int32_t)(value_16bit * normalize_gain);
        
        // Apply high-pass filter (προσωρινά απενεργοποιημένο)
        // normalized = highpass_filter_usb((int16_t)normalized);
        
        // Apply additional gain και clipping
        int32_t amplified = normalized * gain;
        if (amplified > 32767) amplified = 32767;
        if (amplified < -32768) amplified = -32768;
        
        processed_samples_16k[output_count] = (int16_t)amplified;
        
        // Υπολογισμός RMS
        sum += (double)(processed_samples_16k[output_count] * processed_samples_16k[output_count]);
        sum2 += (double)(processed_samples_16k[output_count] ); // Για normalization
        output_count++;
    }
    
    
    
    // Cleanup
    heap_caps_free(temp_samples);
    
    // ===== ΒΗΜΑ 3: RMS CALCULATION =====
    if (output_count > 0) {
        double mean = (sum / (double)output_count);
        *rms = sqrt(mean);
    } else {
        *rms = 0;
    }
    
    taskEXIT_CRITICAL(&mux_usb);
    // Debug log για normalization
    ESP_LOGW(TAG_USB_AUDIO, "DC offset: %ld, Max amplitude: %ld, Normalize gain: %.2f AVG: %.2f, RMS: %.2f", 
             dc_offset, max_abs, normalize_gain, sum2/output_count, *rms);
    return output_count;
}



// ===== USB MICROPHONE CALLBACK =====
static void mic_frame_cb(mic_frame_t *frame, void *user_ptr)
{
    if (!frame || !frame->data || frame->data_bytes == 0) {
        return;
    }
    
    // Debug timing
    int64_t current_time = esp_timer_get_time();
    if (last_callback_time > 0) {
        int64_t callback_interval = current_time - last_callback_time;
        if (callback_interval > 50000) { // Αν πάνω από 50ms
            ESP_LOGD(TAG_USB_AUDIO, "Long callback interval: %lld ms", callback_interval/1000);
        }
    }
    last_callback_time = current_time;
    total_frames_received++;
    
    // Έλεγχος αν έχουμε αρκετό χώρο στον accumulation buffer
    if (accumulation_buffer && 
        (current_accumulation_bytes + frame->data_bytes) <= accumulation_buffer_size) {
        
        // Αντιγραφή των δεδομένων στον accumulation buffer
        memcpy((uint8_t*)accumulation_buffer + current_accumulation_bytes, 
               frame->data, frame->data_bytes);
        current_accumulation_bytes += frame->data_bytes;
        
        // Debug: Εκτύπωση progress κάθε 20 frames
        if (total_frames_received % 20 == 0) {
            ESP_LOGD(TAG_USB_AUDIO, "Callback: Frame %d, collected %d/%d bytes", 
                     total_frames_received, current_accumulation_bytes, target_accumulation_bytes);
        }
        
        // Έλεγχος αν έχουμε συλλέξει αρκετά δεδομένα
        if (current_accumulation_bytes >= target_accumulation_bytes) {
            buffer_ready_for_processing = true;
            ESP_LOGI(TAG_USB_AUDIO, "✅ Buffer ready! Collected %d bytes in %d frames", 
                     current_accumulation_bytes, total_frames_received);
        }
    } else {
        ESP_LOGW(TAG_USB_AUDIO, "Accumulation buffer overflow! Frame size: %ld, available: %d", 
                 frame->data_bytes, accumulation_buffer_size - current_accumulation_bytes);
    }
}

// USB streaming state callback
void usb_streaming_state_cb(usb_stream_state_t state, void *arg)
{
    switch (state) {
        case STREAM_CONNECTED:
            ESP_LOGI(TAG_USB_AUDIO, "Device connected");
            usb_device_connected = true;
            break;
        case STREAM_DISCONNECTED:
            ESP_LOGI(TAG_USB_AUDIO, "Device disconnected");
            usb_device_connected = false;
            usb_streaming_active = false;
            // Reset accumulation
            current_accumulation_bytes = 0;
            buffer_ready_for_processing = false;
            break;
        default:
            ESP_LOGE(TAG_USB_AUDIO, "Unknown USB streaming event: %d", state);
            break;
    }
}

// ===== USB AUDIO INITIALIZATION =====
esp_err_t usb_audio_init(int record_time, uint8_t sample_bits)
{
    ESP_LOGI(TAG_USB_AUDIO, "Initializing USB Audio with CALLBACK approach");

    // Reset state variables
    usb_device_connected = false;
    usb_streaming_active = false;
    current_accumulation_bytes = 0;
    buffer_ready_for_processing = false;
    total_frames_received = 0;

    // ===== ΚΛΕΙΔΙ: ΕΝΕΡΓΟΠΟΙΗΣΗ CALLBACK =====
    uac_config_t uac_config = {
        // Μικρόφωνο με CALLBACK
        .mic_ch_num = 1,                      
        .mic_bit_resolution = sample_bits,            
        .mic_samples_frequence = 48000,      
        .mic_buf_size = 48000 * 2,              // 2 δευτερόλεπτα buffer
        .mic_cb = &mic_frame_cb,                // ✅ ΚΛΕΙΔΙ: Ενεργοποίηση callback
        .mic_cb_arg = NULL,

        // Ηχείο (δεν χρησιμοποιείται)
        .spk_ch_num = 0,
        .spk_bit_resolution = 0,
        .spk_samples_frequence = 0,
        .spk_buf_size = 0,
        .flags = 0,
    };

    esp_err_t ret = uac_streaming_config(&uac_config);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG_USB_AUDIO, "Failed to configure UAC streaming: %s", esp_err_to_name(ret));
        return ret;
    }

    ESP_LOGI(TAG_USB_AUDIO, "USB Audio initialization complete with CALLBACK");
    return ESP_OK;
}

// ===== INITIALIZE ACCUMULATION BUFFER =====
static esp_err_t init_accumulation_buffer(size_t record_time)
{
    // Υπολογισμός buffer size για record_time δευτερόλεπτα στα 48kHz
    target_accumulation_bytes = 48000 * record_time * sizeof(int16_t);
    accumulation_buffer_size = target_accumulation_bytes + (1024 * 16); // Extra space για safety
    
    if (accumulation_buffer != NULL) {
        heap_caps_free(accumulation_buffer);
    }
    
    accumulation_buffer = (int16_t*)heap_caps_malloc(accumulation_buffer_size, MALLOC_CAP_SPIRAM);
    if (accumulation_buffer == NULL) {
        ESP_LOGE(TAG_USB_AUDIO, "Failed to allocate accumulation buffer");
        return ESP_ERR_NO_MEM;
    }
    
    current_accumulation_bytes = 0;
    buffer_ready_for_processing = false;
    
    ESP_LOGI(TAG_USB_AUDIO, "Accumulation buffer initialized: %d bytes target, %d bytes allocated for %d seconds", 
             target_accumulation_bytes, accumulation_buffer_size, record_time);
    return ESP_OK;
}

// ===== MAIN TASK - CALLBACK BASED =====
void task_record_and_encode_audio_usb(void* pv_usb_audio_params)
{
    task_params_t_usb_audio* params = (task_params_t_usb_audio*)pv_usb_audio_params;
    Rec_config_file_t *rec_config_file = params->rec_config_file;
    RingbufHandle_t ring_buffer_handle = (RingbufHandle_t)params->ring_buffer_handle;

    // Store global references για το callback
    global_ring_buffer = ring_buffer_handle;
    global_rec_config = rec_config_file;

    ESP_LOGI(TAG_USB_AUDIO, "Starting USB audio recording with CALLBACK");

    // Initialize accumulation buffer
    if (init_accumulation_buffer(rec_config_file->record_time) != ESP_OK) {
        ESP_LOGE(TAG_USB_AUDIO, "Failed to initialize accumulation buffer");
        vTaskDelete(NULL);
        return;
    }

    // ===== USB INITIALIZATION =====
    ESP_ERROR_CHECK(usb_streaming_state_register(&usb_streaming_state_cb, NULL));
    esp_err_t ret = usb_streaming_start();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG_USB_AUDIO, "USB streaming failed to start: %s", esp_err_to_name(ret));
        vTaskDelete(NULL);
        return;
    }

    vTaskDelay(pdMS_TO_TICKS(2000)); // Wait for initialization
    ESP_ERROR_CHECK(usb_streaming_connect_wait(portMAX_DELAY));
    ESP_LOGI(TAG_USB_AUDIO, "USB device connected");

    // ===== USB STREAMING ACTIVATION =====
    ESP_LOGI(TAG_USB_AUDIO, "Checking USB streaming state...");
    
    // Πρώτα δοκιμάζουμε να βάλουμε την ένταση - αν δουλέψει, το streaming είναι ήδη ενεργό
    ret = usb_streaming_control(STREAM_UAC_MIC, CTRL_UAC_VOLUME, (void *)MIC_VOLUME);
    if (ret == ESP_OK) {
        ESP_LOGI(TAG_USB_AUDIO, "USB streaming already active, volume set");
        usb_streaming_active = true;
    } else {
        // Αν η ένταση απέτυχε, προσπαθούμε να ενεργοποιήσουμε το streaming
        ESP_LOGI(TAG_USB_AUDIO, "Attempting to activate USB streaming...");
        ret = usb_streaming_control(STREAM_UAC_MIC, CTRL_RESUME, NULL);
        if (ret == ESP_OK) {
            ESP_LOGI(TAG_USB_AUDIO, "USB streaming activated successfully");
            usb_streaming_active = true;
            
            // Τώρα βάζουμε την ένταση
            vTaskDelay(pdMS_TO_TICKS(100)); // Μικρή παύση για σταθεροποίηση
            ret = usb_streaming_control(STREAM_UAC_MIC, CTRL_UAC_VOLUME, (void *)MIC_VOLUME);
            if (ret != ESP_OK) {
                ESP_LOGW(TAG_USB_AUDIO, "Failed to set volume: %s", esp_err_to_name(ret));
            }
        } else if (ret == ESP_ERR_INVALID_STATE) {
            ESP_LOGW(TAG_USB_AUDIO, "Streaming already running, continuing...");
            usb_streaming_active = true;
            
            // Δοκιμάζουμε πάλι την ένταση αφού περιμένουμε λίγο
            vTaskDelay(pdMS_TO_TICKS(500));
            ret = usb_streaming_control(STREAM_UAC_MIC, CTRL_UAC_VOLUME, (void *)MIC_VOLUME);
            if (ret != ESP_OK) {
                ESP_LOGW(TAG_USB_AUDIO, "Failed to set volume on running stream: %s", esp_err_to_name(ret));
            }
        } else {
            ESP_LOGE(TAG_USB_AUDIO, "Failed to activate USB streaming: %s", esp_err_to_name(ret));
            vTaskDelete(NULL);
            return;
        }
    }
    
    if (!usb_streaming_active) {
        ESP_LOGE(TAG_USB_AUDIO, "USB streaming is not active, cannot proceed");
        vTaskDelete(NULL);
        return;
    }
    
    ESP_LOGI(TAG_USB_AUDIO, "USB streaming ready, CALLBACK will handle data collection...");

    // ===== MAIN LOOP - ΠΕΡΙΜΕΝΕΙ ΤΟ CALLBACK =====
    int cycle_count = 0;
    while(true) {
        int64_t start_mic = esp_timer_get_time();
        cycle_count++;
        
        // Περιμένουμε το callback να συλλέξει αρκετά δεδομένα
        int wait_cycles = 0;
        const int max_wait_cycles = 200; // 10 δευτερόλεπτα maximum wait
        
        while (!buffer_ready_for_processing && usb_device_connected && wait_cycles < max_wait_cycles) {
            vTaskDelay(pdMS_TO_TICKS(50)); // Check κάθε 50ms
            wait_cycles++;
            
            // Debug: Εκτύπωση progress κάθε 2 δευτερόλεπτα
            if (wait_cycles % 40 == 0) {
                ESP_LOGI(TAG_USB_AUDIO, "Waiting for data... Collected %d/%d bytes (%.1f%%)", 
                         current_accumulation_bytes, target_accumulation_bytes,
                         (float)current_accumulation_bytes * 100.0f / target_accumulation_bytes);
            }
        }
        
        if (!usb_device_connected) {
            ESP_LOGW(TAG_USB_AUDIO, "USB device disconnected, waiting...");
            vTaskDelay(pdMS_TO_TICKS(2000));
            continue;
        }
        
        if (wait_cycles >= max_wait_cycles) {
            ESP_LOGW(TAG_USB_AUDIO, "Timeout waiting for audio data. Resetting...");
            current_accumulation_bytes = 0;
            buffer_ready_for_processing = false;
            total_frames_received = 0;
            continue;
        }
        
        // ===== ΕΠΕΞΕΡΓΑΣΙΑ ΔΕΔΟΜΕΝΩΝ =====
        if (buffer_ready_for_processing && current_accumulation_bytes > 0) {
            size_t usb_samples_count = current_accumulation_bytes / sizeof(uint16_t);
            size_t expected_16k_samples = usb_samples_count / 3; // 3:1 ratio downsampling
            
            ESP_LOGI(TAG_USB_AUDIO, "Cycle %d: Processing %d bytes (%d samples 48kHz) -> ~%d samples 16kHz", 
                     cycle_count, current_accumulation_bytes, usb_samples_count, expected_16k_samples);
            
            // Allocate processing buffer
            int16_t* audio_buffer = (int16_t*)heap_caps_malloc(expected_16k_samples * sizeof(int16_t), MALLOC_CAP_SPIRAM);
            
            if (audio_buffer != NULL) {  
                double rms = 0;
                
                size_t actual_samples_16k = process_usb_audio_data_with_downsampling(
                    accumulation_buffer, usb_samples_count, audio_buffer, &rms);
                
                ESP_LOGI(TAG_USB_AUDIO, "Processed to %d samples (16kHz), RMS=%.2f, Threshold=%.2f", 
                         actual_samples_16k, rms, rec_config_file->rms);
                
                 
                if (rms >= rec_config_file->rms) {
                    ESP_LOGW(TAG_USB_AUDIO, "✅ RMS %.2f >= Threshold %.2f - Sending to ring buffer", 
                             rms, rec_config_file->rms);
                    
                   // size_t final_bytes = actual_samples_16k * sizeof(int16_t);
                   // BaseType_t ret_ring = xRingbufferSend(ring_buffer_handle, audio_buffer, final_bytes, pdMS_TO_TICKS(10));
                   size_t final_bytes =current_accumulation_bytes;
                   BaseType_t ret_ring = xRingbufferSend(ring_buffer_handle, accumulation_buffer, final_bytes, pdMS_TO_TICKS(10));
                   if (ret_ring != pdPASS) {
                        ESP_LOGE(TAG_USB_AUDIO, "Failed to send item to ring buffer");
                    } else {
                        ESP_LOGI(TAG_USB_AUDIO, "📤 Sent %d bytes to ring buffer (16kHz)", final_bytes);
                        size_t free_space = xRingbufferGetCurFreeSize(ring_buffer_handle);  
                        ESP_LOGI(TAG_USB_AUDIO, "***** Ring Buffer free space  %d *****", free_space);
                    }
                } else {
                    ESP_LOGW(TAG_USB_AUDIO, "❌ RMS too low %.2f < Threshold %.2f", rms, rec_config_file->rms);
                }
                
                heap_caps_free(audio_buffer);
            } else {
                ESP_LOGE(TAG_USB_AUDIO, "Failed to allocate audio buffer");
            }
            
            // Reset accumulation buffer για το επόμενο recording
            current_accumulation_bytes = 0;
            buffer_ready_for_processing = false;
            total_frames_received = 0;
        }
        
        vTaskDelay(pdMS_TO_TICKS(100));
        time_message_usb(start_mic, "usb_audio_cycle");
        
        // Memory check κάθε 10 cycles
        if (cycle_count % 10 == 0) {
            print_free_heap_usb();
        }
    }
    
    // Cleanup
    if (accumulation_buffer) {
        heap_caps_free(accumulation_buffer);
        accumulation_buffer = NULL;
    }
    
    vTaskDelete(NULL);  
}

// ===== STATUS CHECKING FUNCTIONS =====
bool usb_audio_is_connected(void)
{
    return usb_device_connected;
}

bool usb_audio_is_streaming(void)
{
    return usb_streaming_active && usb_device_connected;
}

// ===== CLEANUP FUNCTION =====
void usb_audio_cleanup(void)
{
    usb_streaming_active = false;
    usb_device_connected = false;
    
    if (accumulation_buffer) {
        heap_caps_free(accumulation_buffer);
        accumulation_buffer = NULL;
    }
    
    current_accumulation_bytes = 0;
    buffer_ready_for_processing = false;
    total_frames_received = 0;
    
    usb_streaming_stop();
    ESP_LOGI(TAG_USB_AUDIO, "USB Audio cleanup complete");
}