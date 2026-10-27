#include "confing_file.h"
//#include "cJSON.h"

void write_default_values_to_rec_config_file(Rec_config_file_t *rec_config_file){
    rec_config_file->index=0;
    rec_config_file->version_config=0.1;
    //strcpy(rec_config_file->last_image_file_name,"none");
#if DEVICE_ID == DEVICE_ESP32S3_1
    strcpy(rec_config_file->mqtt_broker,"your-broker.example.com");
    rec_config_file->mqtt_port = 8883;
    strcpy(rec_config_file->mqtt_client_id, "esp32s3-1");
    strcpy(rec_config_file->mqtt_password , "your_mqtt_password");
    rec_config_file->rms = 25.0;
    rec_config_file->sample_rate=16000;
    rec_config_file->sample_bits=16;
    rec_config_file->record_time=6;
   // strcpy(rec_config_file->wifi_ssid , "your_wifi_ssid");
   // strcpy(rec_config_file->wifi_password , "your_wifi_password");
    strcpy(rec_config_file->wifi_ssid , WIFI_SSID);
    strcpy(rec_config_file->wifi_password , WIFI_PASS);
#elif DEVICE_ID == DEVICE_ESP32S3_2
    strcpy(rec_config_file->mqtt_broker,"your-broker.example.com");
    rec_config_file->mqtt_port = 8883;
    strcpy(rec_config_file->mqtt_client_id, "esp32s3-2");
    strcpy(rec_config_file->mqtt_password , "your_mqtt_password");
    rec_config_file->rms = 25.0;
    rec_config_file->sample_rate=16000;
    rec_config_file->sample_bits=16;
    rec_config_file->record_time=15;
    strcpy(rec_config_file->wifi_ssid , WIFI_SSID);
    strcpy(rec_config_file->wifi_password , WIFI_PASS);
#elif DEVICE_ID == DEVICE_ESP32S3_3
    strcpy(rec_config_file->mqtt_broker,"your-broker.example.com");
    rec_config_file->mqtt_port = 8883;
    strcpy(rec_config_file->mqtt_client_id, "esp32s3-3");
    strcpy(rec_config_file->mqtt_password , "your_mqtt_password");
    rec_config_file->rms = 60.0;
    rec_config_file->sample_rate=48000;
    rec_config_file->sample_bits=16;
    rec_config_file->record_time=5;
    strcpy(rec_config_file->wifi_ssid , WIFI_SSID);
    strcpy(rec_config_file->wifi_password , WIFI_PASS);
#elif DEVICE_ID == DEVICE_ESP32S3_4
    strcpy(rec_config_file->mqtt_broker,"your-broker.example.com");
    rec_config_file->mqtt_port = 8883;
    strcpy(rec_config_file->mqtt_client_id, "esp32s3-4");
    strcpy(rec_config_file->mqtt_password , "your_mqtt_password");
    rec_config_file->rms = 60.0;
    rec_config_file->sample_rate=48000;
    rec_config_file->sample_bits=16;
    rec_config_file->record_time=5;
    strcpy(rec_config_file->wifi_ssid , WIFI_SSID);
    strcpy(rec_config_file->wifi_password , WIFI_PASS);
#endif
}

esp_err_t read_config_string(const char *config_json_string,Rec_config_file_t *rec_config_file){
    //ESP_LOGI(TAGCONFIG_FILE, "read_config_string input string %s",config_json_string);
    cJSON *root = cJSON_Parse(config_json_string);
    if (root!=NULL){
        rec_config_file->index = cJSON_GetObjectItem(root,"index")->valueint;
        rec_config_file->version_config= cJSON_GetObjectItem(root,"version_config")->valuedouble;
        strcpy(rec_config_file->mqtt_broker , cJSON_GetObjectItem(root,"mqtt_broker")->valuestring);
        rec_config_file->mqtt_port = cJSON_GetObjectItem(root,"mqtt_port")->valueint;
        strcpy(rec_config_file->mqtt_client_id , cJSON_GetObjectItem(root,"mqtt_client_id")->valuestring);
        strcpy(rec_config_file->mqtt_password , cJSON_GetObjectItem(root,"mqtt_password")->valuestring);
        rec_config_file->rms = cJSON_GetObjectItem(root,"rms")->valuedouble;
        rec_config_file->sample_rate=cJSON_GetObjectItem(root,"sample_rate")->valueint;
        rec_config_file->sample_bits=cJSON_GetObjectItem(root,"sample_bits")->valueint;
        rec_config_file->record_time=cJSON_GetObjectItem(root,"record_time")->valueint;
        sprintf(rec_config_file->wifi_ssid , cJSON_GetObjectItem(root,"wifi_ssid")->valuestring);
        sprintf(rec_config_file->wifi_password , cJSON_GetObjectItem(root,"wifi_password")->valuestring);
    //  ESP_LOGI(TAGCONFIG_FILE, "read_config_string output string %s",cJSON_Print(root));
        cJSON_Delete(root);
        return ESP_OK;
    }
    return ESP_FAIL;
}


esp_err_t write_config_string(char *config_json_string,Rec_config_file_t *rec_config_file){
    cJSON *root = cJSON_CreateObject();
    if (root!=NULL){
        cJSON_AddNumberToObject(root, "index", rec_config_file->index);
        cJSON_AddNumberToObject(root, "version_config", rec_config_file->version_config);
        cJSON_AddStringToObject(root, "mqtt_broker", rec_config_file->mqtt_broker);
        cJSON_AddNumberToObject(root, "mqtt_port", rec_config_file->mqtt_port);
        cJSON_AddStringToObject(root, "mqtt_client_id", rec_config_file->mqtt_client_id);
        cJSON_AddStringToObject(root, "mqtt_password", rec_config_file->mqtt_password);
        cJSON_AddNumberToObject(root, "rms", rec_config_file->rms);
        cJSON_AddStringToObject(root, "wifi_ssid", rec_config_file->wifi_ssid);
        cJSON_AddStringToObject(root, "wifi_password", rec_config_file->wifi_password);

        cJSON_AddNumberToObject(root, "sample_rate", rec_config_file->sample_rate);
        cJSON_AddNumberToObject(root, "sample_bits", rec_config_file->sample_bits);
        cJSON_AddNumberToObject(root, "record_time", rec_config_file->record_time);
        strcpy(config_json_string, cJSON_Print(root));
        //ESP_LOGI(TAGCONFIG_FILE, "my_json_string\n%s",config_json_string);
        cJSON_Delete(root);
        return ESP_OK;
    }
    return ESP_FAIL;
}


esp_err_t read_config_nvs(Rec_config_file_t *rec_config_file){
    nvs_handle_t my_handle;
    esp_err_t err = nvs_open(STORAGE_NAMESPACE_CONFIG, NVS_READWRITE, &my_handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAGCONFIG_FILE,"Error Open Confing nvs");
        return err;
    }
    size_t tmp_configsize=0;
    err = nvs_get_blob(my_handle, key_con,NULL , &tmp_configsize);
    if(tmp_configsize==0){
        ESP_LOGE(TAGCONFIG_FILE,"Read nvs Nothin in Config to read ");
        return ESP_FAIL;
    }
    else{
        char config_json_string[(int)tmp_configsize];
        err = nvs_get_blob(my_handle, key_con,config_json_string , &tmp_configsize);
        if (err != ESP_OK) {
            ESP_LOGE(TAGCONFIG_FILE,"Error Get Blob  Config nvs");
            return err;
        }
        ESP_LOGI(TAGCONFIG_FILE,"Read  Config = %s \n ",config_json_string);
        if (ESP_FAIL == read_config_string(config_json_string,rec_config_file))
        {
            ESP_LOGE(TAGCONFIG_FILE,"Error read config file");
            return ESP_FAIL;
        }
    }
    nvs_close(my_handle);
    ESP_LOGI(TAGCONFIG_FILE,"\n Read  Config nvs size= %d \n ",sizeof(rec_config_file));
    return ESP_OK;
}

esp_err_t save_config_nvs( Rec_config_file_t *rec_config_file){
     nvs_handle_t my_handle;
    esp_err_t err = nvs_open(STORAGE_NAMESPACE_CONFIG, NVS_READWRITE, &my_handle);
    if (err != ESP_OK){
        ESP_LOGE(TAGCONFIG_FILE,"Error Open  Config nvs");        
        return err;
    } 
    char config_json_string[sizeof(Rec_config_file_t)];
    write_config_string(config_json_string,rec_config_file);
    if(strlen(config_json_string)<=0){
        ESP_LOGE(TAGCONFIG_FILE,"Nothing to Save");
        return ESP_FAIL;
    }else{
        err = nvs_set_blob(my_handle, key_con, config_json_string, strlen(config_json_string));   
        if (err != ESP_OK) {
            ESP_LOGE(TAGCONFIG_FILE,"Can not write Config nvs");
            return err;
        }
        err = nvs_commit(my_handle);
        if (err != ESP_OK) {
            ESP_LOGE(TAGCONFIG_FILE,"Can not commit Config nvs !!!!!!!!!!!");
            return err;
        }
    }
    nvs_close(my_handle);
    ESP_LOGI(TAGCONFIG_FILE,"\n Save Config nvs \n");
    return ESP_OK;
}

esp_err_t save_string_nvs( char *config_json_string,size_t config_json_string_len){
     nvs_handle_t my_handle;
    esp_err_t err = nvs_open(STORAGE_NAMESPACE_CONFIG, NVS_READWRITE, &my_handle);
    if (err != ESP_OK){
        ESP_LOGE(TAGCONFIG_FILE,"Error Open  Config nvs");        
        return err;
    } 
    
    if(config_json_string_len == 0){
        ESP_LOGE(TAGCONFIG_FILE,"Nothing to Save");
        return ESP_FAIL;
    }else{
        err = nvs_set_blob(my_handle, key_con, config_json_string, config_json_string_len);   
        if (err != ESP_OK) {
            ESP_LOGE(TAGCONFIG_FILE,"Can not write Config nvs");
            return err;
        }
        err = nvs_commit(my_handle);
        if (err != ESP_OK) {
            ESP_LOGE(TAGCONFIG_FILE,"Can not commit Config nvs !!!!!!!!!!!");
            return err;
        }
    }
    nvs_close(my_handle);
    ESP_LOGI(TAGCONFIG_FILE,"\n Save Config nvs \n");
    return ESP_OK;
}

void print_config_file(Rec_config_file_t *rec_config_file){
    ESP_LOGI(TAGCONFIG_FILE, "index %d",rec_config_file->index);
    ESP_LOGI(TAGCONFIG_FILE, "version_config %f",rec_config_file->version_config);
    ESP_LOGI(TAGCONFIG_FILE, "mqtt_broker %s",rec_config_file->mqtt_broker);
    ESP_LOGI(TAGCONFIG_FILE, "mqtt_port %d",rec_config_file->mqtt_port);
    ESP_LOGI(TAGCONFIG_FILE, "mqtt_client_id %s",rec_config_file->mqtt_client_id);
    ESP_LOGI(TAGCONFIG_FILE, "mqtt_password %s",rec_config_file->mqtt_password);
    ESP_LOGI(TAGCONFIG_FILE, "rms %f",rec_config_file->rms);
    ESP_LOGI(TAGCONFIG_FILE, "wifi_ssid %s",rec_config_file->wifi_ssid);
    ESP_LOGI(TAGCONFIG_FILE, "wifi_password %s",rec_config_file->wifi_password);
    ESP_LOGI(TAGCONFIG_FILE, "sample_rate %d",rec_config_file->sample_rate);
    ESP_LOGI(TAGCONFIG_FILE, "sample_bits %d",rec_config_file->sample_bits);
    ESP_LOGI(TAGCONFIG_FILE, "record_time %d",rec_config_file->record_time);
}