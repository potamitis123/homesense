#include "wifi_manager.h"
#include "esp_wifi.h"
#include "esp_log.h"
#include "esp_event.h"
#include "nvs_flash.h"


static const char *TAG = "WIFI_MANAGER";
static int s_retry_num = 0;

static EventGroupHandle_t s_wifi_event_group=NULL;



static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                             int32_t event_id, void *event_data) {
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
       
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        if (s_retry_num < MAXIMUM_RETRY) {
            esp_wifi_connect();
            s_retry_num++;
            ESP_LOGI(TAG, "Retry to connect to the AP");
        } else {
            xEventGroupSetBits(s_wifi_event_group, WIFI_FAIL_BIT);
        }
        ESP_LOGI(TAG,"Connect to the AP fail");
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t* event = (ip_event_got_ip_t*) event_data;
        ESP_LOGI(TAG, "Got ip:" IPSTR, IP2STR(&event->ip_info.ip));
        s_retry_num = 0;
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

esp_err_t wifi_manager_init(void) {
    s_wifi_event_group = xEventGroupCreate();
    esp_err_t ret;
    ret=esp_netif_init();
    if(ret==ESP_FAIL){
        ESP_LOGE(TAG,"Error Init");
        return ret;
    }
    ret=esp_event_loop_create_default();
    if(ret!=ESP_OK){
        if(ret==ESP_FAIL)
            ESP_LOGE(TAG,"Loop create Error ESP_FAIL");
        else if (ret==ESP_ERR_NO_MEM)
            ESP_LOGE(TAG,"Loop create Error ESP_ERR_NO_MEM");
        else if (ret==ESP_ERR_INVALID_STATE)
            ESP_LOGE(TAG,"Loop create Error ESP_ERR_INVALID_STATE");    
        return ESP_FAIL;
    }
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ret=esp_wifi_init(&cfg);
    if(ret!=ESP_OK){
        ESP_LOGE(TAG,"esp_wifi_init Error");
        return ESP_FAIL;
    }
    return ESP_OK;
}

bool wifi_connect(Rec_config_file_t *rec_config_file) {
   
    bool ret = false;

    esp_event_handler_instance_t instance_any_id;
    esp_event_handler_instance_t instance_got_ip;
    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT,
                                                      ESP_EVENT_ANY_ID,
                                                      &wifi_event_handler,
                                                      NULL,
                                                      &instance_any_id));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT,
                                                      IP_EVENT_STA_GOT_IP,
                                                      &wifi_event_handler,
                                                      NULL,
                                                      &instance_got_ip));

    // wifi_config_t wifi_config = {
    //     .sta = {
    //         .ssid = rec_config_file->wifi_ssid,
    //         .password = rec_config_file->wifi_password,
    //     },
    //  };
    esp_err_t ret1 = esp_wifi_set_mode(WIFI_MODE_STA);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to set WiFi mode: %s", esp_err_to_name(ret));
        return ret1;
    }
    wifi_config_t wifi_config={};
    strcpy((char *)wifi_config.sta.ssid , rec_config_file->wifi_ssid);
    strcpy((char *)wifi_config.sta.password , rec_config_file->wifi_password);
    ESP_LOGI(TAG,"SSID:%s  %s %s",rec_config_file->wifi_ssid,wifi_config.sta.ssid,wifi_config.sta.password);
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG, "wifi_init_sta finished.");

    // Περιμένουμε για τη σύνδεση
    EventBits_t bits = xEventGroupWaitBits(s_wifi_event_group,
            WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
            pdFALSE,
            pdFALSE,
            portMAX_DELAY);

    if (bits & WIFI_CONNECTED_BIT) {
        ESP_LOGI(TAG, "connected to ap SSID:%s", rec_config_file->wifi_ssid);
        ret = true;
    } else if (bits & WIFI_FAIL_BIT) {
        ESP_LOGI(TAG, "Failed to connect to SSID:%s", rec_config_file->wifi_ssid);
        ret = false;
    } else {
        ESP_LOGE(TAG, "UNEXPECTED EVENT");
        ret = false;
    }

    return ret;
}

