#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "ai_agent.h"
#include "cJSON.h"
#include "esp_crt_bundle.h"
#include "esp_event.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_websocket_client.h"
#include "mbedtls/base64.h"
#include "settings.h"

static const char *TAG = "AI_AGENT";

#define HTTP_RX_LIMIT (16 * 1024)

typedef struct {
    char *api_key;
    char *system_prompt;

    esp_websocket_client_handle_t ws;
    ai_agent_realtime_audio_cb_t on_audio;
    ai_agent_realtime_done_cb_t on_done;
    void *ctx;
    bool ws_open;
} ai_agent_t;

static ai_agent_t s_agent;

esp_err_t ai_agent_init(const ai_agent_cfg_t *cfg)
{
    if (cfg == NULL || cfg->api_key == NULL || cfg->api_key[0] == '\0') {
        return ESP_ERR_INVALID_ARG;
    }
    ai_agent_deinit();
    s_agent.api_key = strdup(cfg->api_key);
    s_agent.system_prompt = strdup(cfg->system_prompt ? cfg->system_prompt
                                                      : OPENAI_SYSTEM_PROMPT);
    if (s_agent.api_key == NULL || s_agent.system_prompt == NULL) {
        ai_agent_deinit();
        return ESP_ERR_NO_MEM;
    }
    ESP_LOGI(TAG, "OpenAI agent ready");
    return ESP_OK;
}

void ai_agent_deinit(void)
{
    ai_agent_realtime_stop();
    free(s_agent.api_key);
    free(s_agent.system_prompt);
    s_agent.api_key = NULL;
    s_agent.system_prompt = NULL;
}

bool ai_agent_is_ready(void)
{
    return s_agent.api_key != NULL;
}

/* ── Vision ─────────────────────────────────────────────────────────── */

static char *base64_dup(const uint8_t *data, size_t len, size_t *out_len)
{
    size_t needed = 0;
    mbedtls_base64_encode(NULL, 0, &needed, data, len);
    char *buf = malloc(needed + 1);
    if (buf == NULL) {
        return NULL;
    }
    size_t written = 0;
    if (mbedtls_base64_encode((unsigned char *)buf, needed, &written, data,
                              len) != 0) {
        free(buf);
        return NULL;
    }
    buf[written] = '\0';
    if (out_len) {
        *out_len = written;
    }
    return buf;
}

/* Builds the chat-completions body: a system turn carrying the standing
 * instructions and a user turn carrying the frame as a data URL. */
static char *build_vision_request(const uint8_t *jpeg, size_t jpeg_len)
{
    size_t b64_len = 0;
    char *b64 = base64_dup(jpeg, jpeg_len, &b64_len);
    if (b64 == NULL) {
        return NULL;
    }
    char *data_url = malloc(b64_len + 32);
    if (data_url == NULL) {
        free(b64);
        return NULL;
    }
    sprintf(data_url, "data:image/jpeg;base64,%s", b64);
    free(b64);

    cJSON *root = cJSON_CreateObject();
    cJSON *messages = cJSON_AddArrayToObject(root, "messages");
    cJSON_AddStringToObject(root, "model", OPENAI_VISION_MODEL);
    cJSON_AddNumberToObject(root, "max_tokens", 120);

    cJSON *system = cJSON_CreateObject();
    cJSON_AddStringToObject(system, "role", "system");
    cJSON_AddStringToObject(system, "content", s_agent.system_prompt);
    cJSON_AddItemToArray(messages, system);

    cJSON *user = cJSON_CreateObject();
    cJSON_AddStringToObject(user, "role", "user");
    cJSON *content = cJSON_AddArrayToObject(user, "content");

    cJSON *text_part = cJSON_CreateObject();
    cJSON_AddStringToObject(text_part, "type", "text");
    cJSON_AddStringToObject(text_part, "text",
                            "Describe who or what is at the door, briefly.");
    cJSON_AddItemToArray(content, text_part);

    cJSON *image_part = cJSON_CreateObject();
    cJSON_AddStringToObject(image_part, "type", "image_url");
    cJSON *image_url = cJSON_AddObjectToObject(image_part, "image_url");
    cJSON_AddStringToObject(image_url, "url", data_url);
    cJSON_AddItemToArray(content, image_part);
    cJSON_AddItemToArray(messages, user);

    free(data_url);
    char *body = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    return body;
}

static esp_err_t parse_vision_reply(const char *json, char *out,
                                    size_t out_size)
{
    cJSON *root = cJSON_Parse(json);
    if (root == NULL) {
        return ESP_ERR_INVALID_RESPONSE;
    }
    esp_err_t ret = ESP_ERR_INVALID_RESPONSE;
    const cJSON *choices = cJSON_GetObjectItem(root, "choices");
    const cJSON *first = cJSON_GetArrayItem(choices, 0);
    const cJSON *message = cJSON_GetObjectItem(first, "message");
    const cJSON *content = cJSON_GetObjectItem(message, "content");
    if (cJSON_IsString(content)) {
        snprintf(out, out_size, "%s", content->valuestring);
        ret = ESP_OK;
    } else {
        const cJSON *error = cJSON_GetObjectItem(root, "error");
        const cJSON *msg = cJSON_GetObjectItem(error, "message");
        ESP_LOGE(TAG, "Vision request rejected: %s",
                 cJSON_IsString(msg) ? msg->valuestring : "unknown error");
    }
    cJSON_Delete(root);
    return ret;
}

esp_err_t ai_agent_describe_frame(const uint8_t *jpeg, size_t jpeg_len,
                                  char *out, size_t out_size)
{
    if (!ai_agent_is_ready()) {
        return ESP_ERR_INVALID_STATE;
    }
    if (jpeg == NULL || jpeg_len == 0 || out == NULL || out_size == 0) {
        return ESP_ERR_INVALID_ARG;
    }

    char *body = build_vision_request(jpeg, jpeg_len);
    if (body == NULL) {
        return ESP_ERR_NO_MEM;
    }
    char auth[128];
    snprintf(auth, sizeof(auth), "Bearer %s", s_agent.api_key);

    esp_http_client_config_t cfg = {
        .url = OPENAI_BASE_URL "/chat/completions",
        .method = HTTP_METHOD_POST,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .timeout_ms = 20000,
        .buffer_size_tx = 2048,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (client == NULL) {
        free(body);
        return ESP_FAIL;
    }
    esp_http_client_set_header(client, "Content-Type", "application/json");
    esp_http_client_set_header(client, "Authorization", auth);

    esp_err_t ret = ESP_FAIL;
    char *rx = NULL;
    do {
        if (esp_http_client_open(client, (int)strlen(body)) != ESP_OK) {
            break;
        }
        if (esp_http_client_write(client, body, (int)strlen(body)) < 0) {
            break;
        }
        int64_t len = esp_http_client_fetch_headers(client);
        int cap = (len > 0 && len < HTTP_RX_LIMIT) ? (int)len : HTTP_RX_LIMIT;
        rx = malloc((size_t)cap + 1);
        if (rx == NULL) {
            ret = ESP_ERR_NO_MEM;
            break;
        }
        int read = esp_http_client_read_response(client, rx, cap);
        if (read < 0) {
            break;
        }
        rx[read] = '\0';
        int status = esp_http_client_get_status_code(client);
        if (status != 200) {
            ESP_LOGE(TAG, "Vision HTTP %d: %s", status, rx);
            break;
        }
        ret = parse_vision_reply(rx, out, out_size);
    } while (0);

    free(rx);
    free(body);
    esp_http_client_cleanup(client);
    return ret;
}

/* ── Realtime voice ─────────────────────────────────────────────────── */

/* Pins both directions to G.711 A-law so model audio can be handed to
 * av_render, and capture audio to the model, without touching the samples.
 * Server-side VAD is what lets the model decide when the caller has
 * finished, which is the behaviour a hands-free intercom needs. */
static void realtime_configure_session(void)
{
    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "session.update");
    cJSON *session = cJSON_AddObjectToObject(root, "session");
    cJSON_AddStringToObject(session, "instructions", s_agent.system_prompt);
    cJSON_AddStringToObject(session, "input_audio_format", "g711_alaw");
    cJSON_AddStringToObject(session, "output_audio_format", "g711_alaw");
    cJSON *modalities = cJSON_AddArrayToObject(session, "modalities");
    cJSON_AddItemToArray(modalities, cJSON_CreateString("audio"));
    cJSON_AddItemToArray(modalities, cJSON_CreateString("text"));
    cJSON *vad = cJSON_AddObjectToObject(session, "turn_detection");
    cJSON_AddStringToObject(vad, "type", "server_vad");

    char *payload = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (payload) {
        esp_websocket_client_send_text(s_agent.ws, payload,
                                       (int)strlen(payload), portMAX_DELAY);
        free(payload);
    }
}

static void realtime_on_text(const char *text, int len)
{
    cJSON *root = cJSON_ParseWithLength(text, (size_t)len);
    if (root == NULL) {
        return;
    }
    const cJSON *type = cJSON_GetObjectItem(root, "type");
    if (!cJSON_IsString(type)) {
        cJSON_Delete(root);
        return;
    }

    if (strcmp(type->valuestring, "response.audio.delta") == 0) {
        const cJSON *delta = cJSON_GetObjectItem(root, "delta");
        if (cJSON_IsString(delta) && s_agent.on_audio) {
            size_t b64_len = strlen(delta->valuestring);
            size_t out_len = 0;
            uint8_t *pcm = malloc(b64_len);
            if (pcm != NULL) {
                if (mbedtls_base64_decode(pcm, b64_len, &out_len,
                                          (const unsigned char *)delta->valuestring,
                                          b64_len) == 0) {
                    s_agent.on_audio(pcm, (int)out_len, s_agent.ctx);
                }
                free(pcm);
            }
        }
    } else if (strcmp(type->valuestring, "response.done") == 0) {
        if (s_agent.on_done) {
            s_agent.on_done(s_agent.ctx);
        }
    } else if (strcmp(type->valuestring, "error") == 0) {
        const cJSON *error = cJSON_GetObjectItem(root, "error");
        const cJSON *msg = cJSON_GetObjectItem(error, "message");
        ESP_LOGE(TAG, "Realtime error: %s",
                 cJSON_IsString(msg) ? msg->valuestring : "unknown");
    }
    cJSON_Delete(root);
}

static void realtime_event_handler(void *args, esp_event_base_t base,
                                   int32_t event_id, void *event_data)
{
    esp_websocket_event_data_t *data = (esp_websocket_event_data_t *)event_data;
    switch (event_id) {
    case WEBSOCKET_EVENT_CONNECTED:
        ESP_LOGI(TAG, "Realtime session open");
        s_agent.ws_open = true;
        realtime_configure_session();
        break;
    case WEBSOCKET_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "Realtime session closed");
        s_agent.ws_open = false;
        break;
    case WEBSOCKET_EVENT_DATA:
        if (data->op_code == 0x01 && data->data_len > 0) {
            realtime_on_text(data->data_ptr, data->data_len);
        }
        break;
    default:
        break;
    }
}

esp_err_t ai_agent_realtime_start(ai_agent_realtime_audio_cb_t on_audio,
                                  ai_agent_realtime_done_cb_t on_done,
                                  void *ctx)
{
    if (!ai_agent_is_ready()) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_agent.ws != NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    s_agent.on_audio = on_audio;
    s_agent.on_done = on_done;
    s_agent.ctx = ctx;

    char url[256];
    snprintf(url, sizeof(url), "%s?model=%s", OPENAI_REALTIME_URL,
             OPENAI_REALTIME_MODEL);
    char headers[192];
    snprintf(headers, sizeof(headers),
             "Authorization: Bearer %s\r\nOpenAI-Beta: realtime=v1\r\n",
             s_agent.api_key);

    esp_websocket_client_config_t cfg = {
        .uri = url,
        .headers = headers,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .buffer_size = 8192,
        .network_timeout_ms = 10000,
        .reconnect_timeout_ms = 5000,
    };
    s_agent.ws = esp_websocket_client_init(&cfg);
    if (s_agent.ws == NULL) {
        return ESP_FAIL;
    }
    esp_websocket_register_events(s_agent.ws, WEBSOCKET_EVENT_ANY,
                                  realtime_event_handler, NULL);
    esp_err_t ret = esp_websocket_client_start(s_agent.ws);
    if (ret != ESP_OK) {
        esp_websocket_client_destroy(s_agent.ws);
        s_agent.ws = NULL;
    }
    return ret;
}

esp_err_t ai_agent_realtime_send_audio(const uint8_t *alaw, int len)
{
    if (s_agent.ws == NULL || !s_agent.ws_open) {
        return ESP_ERR_INVALID_STATE;
    }
    size_t b64_len = 0;
    char *b64 = base64_dup(alaw, (size_t)len, &b64_len);
    if (b64 == NULL) {
        return ESP_ERR_NO_MEM;
    }
    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "input_audio_buffer.append");
    cJSON_AddStringToObject(root, "audio", b64);
    char *payload = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    free(b64);
    if (payload == NULL) {
        return ESP_ERR_NO_MEM;
    }
    int sent = esp_websocket_client_send_text(s_agent.ws, payload,
                                              (int)strlen(payload),
                                              portMAX_DELAY);
    free(payload);
    return sent < 0 ? ESP_FAIL : ESP_OK;
}

void ai_agent_realtime_stop(void)
{
    if (s_agent.ws == NULL) {
        return;
    }
    esp_websocket_client_stop(s_agent.ws);
    esp_websocket_client_destroy(s_agent.ws);
    s_agent.ws = NULL;
    s_agent.ws_open = false;
}
