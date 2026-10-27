#ifndef WIFI_MANAGER_H
#define WIFI_MANAGER_H

#include <stdbool.h>
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "confing_file.h"

//#define WIFI_SSID       "your_wifi_ssid"
//#define WIFI_PASSWORD   "your_wifi_password"
#define MAXIMUM_RETRY   5

#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT     BIT1


esp_err_t wifi_manager_init(void);
bool wifi_connect(Rec_config_file_t *rec_config_file);


#endif