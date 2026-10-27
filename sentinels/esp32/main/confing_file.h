#ifndef CONFIG_FILE_H_
#define CONFIG_FILE_H_

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "esp_err.h"
#include <esp_log.h>
#include "cJSON.h"

#include "nvs_flash.h"
#include "nvs.h"
#include "global_heads.h"
#define CONFIG_FILE_NAME "conf.json"
#define STORAGE_NAMESPACE_CONFIG "config"


#ifdef __cplusplus
extern "C" {
#endif
static  char *TAGCONFIG_FILE = "Config File ";

static const char *key_con = "nvs_config";


typedef struct {
    int index;
    double version_config;
    char mqtt_broker[100];
    int mqtt_port;
    char mqtt_client_id[50];
    char mqtt_password[50];
    double rms;
    char wifi_ssid[32];
    char wifi_password[64];
    int sample_rate;
    int sample_bits;
    int record_time;
}Rec_config_file_t;

void write_default_values_to_rec_config_file(Rec_config_file_t *rec_config_file);
esp_err_t read_config_file_json(const char *filename, Rec_config_file_t *rec_config_file);
esp_err_t write_config_file_json(const char *filename, Rec_config_file_t *rec_config_file);
esp_err_t read_config_string(const char *config_json_string,Rec_config_file_t *rec_config_file);
esp_err_t write_config_string(char *config_json_string,Rec_config_file_t *rec_config_file);
esp_err_t save_config_nvs( Rec_config_file_t *rec_config_file);
esp_err_t read_config_nvs( Rec_config_file_t *rec_config_file);
esp_err_t save_string_nvs( char *config_json_string,size_t config_json_string_len);

void print_config_file(Rec_config_file_t *rec_config_file);

#ifdef __cplusplus
}
#endif

#endif