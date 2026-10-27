#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "broker_addr.h"
#include "cJSON.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "mode_arbiter.h"
#include "mqtt_client.h"
#include "mqtt_signaling.h"
#include "settings.h"

/* Presence topics and payloads. Deliberately identical to what the ESP32-S3
 * units publish (sentinel/audiosense_mqtt.h), because the server parses both
 * through the same handler in ast_src/communication/adapters.py. */
#define MQTT_DEVICE_TOPIC_BASE      "devices"
#define MQTT_STATUS_PAYLOAD_ONLINE  "{\"status\":\"online\"}"
#define MQTT_STATUS_PAYLOAD_OFFLINE "{\"status\":\"offline\"}"

/* The dashboard treats a sentinel that has said nothing for five minutes as
 * unconfirmed, so a one-minute beat leaves room for a few lost publishes
 * before the panel goes amber. */
#define PRESENCE_HEARTBEAT_US       (60 * 1000000ULL)

static const char *TAG = "MQTT_SIG";

#define TOPIC_MAX 160

typedef struct {
    esp_mqtt_client_handle_t client;
    esp_peer_signaling_cfg_t cfg;
    esp_peer_signaling_ice_info_t ice_info;

    char sub_topic[TOPIC_MAX];
    char pub_topic[TOPIC_MAX];
    /* Presence. Separate from signaling: the dashboard discovers sentinels
     * by watching devices/+/status, devices/+/ip and devices/+/capabilities,
     * and this unit previously published none of them -- it only ever spoke
     * on its signaling topic, so the app had no way to know it existed. */
    char status_topic[TOPIC_MAX];
    char ip_topic[TOPIC_MAX];
    char caps_topic[TOPIC_MAX];
    esp_timer_handle_t heartbeat;

    /* esp-mqtt splits payloads larger than CONFIG_MQTT_BUFFER_SIZE across
     * several MQTT_EVENT_DATA callbacks. An SDP offer with a handful of ICE
     * candidates runs to several kilobytes, so reassembly is not optional
     * here the way it is for small control messages. */
    char *rx_buf;
    int rx_len;
    int rx_expected;

    bool connected;
    /* Set for an intentional stop so DISCONNECTED does not reconnect, and
     * so a keepalive miss is not treated as "the call is over". */
    bool stopping;
    esp_timer_handle_t reconnect_timer;

    /* Hostname for TLS SNI / CN. esp-mqtt stores the common_name pointer. */
    char tls_name[128];
    /* Dotted IPv4 once cached, otherwise the hostname. Numeric hosts skip
     * lwIP DNS, which is what hangs MQTT after a call (getaddrinfo 202). */
    char connect_host[128];
} mqtt_sig_t;

static void reconnect_timer_cb(void *arg)
{
    mqtt_sig_t *sig = (mqtt_sig_t *)arg;
    if (sig == NULL || sig->stopping || sig->connected || sig->client == NULL) {
        return;
    }
    ESP_LOGI(TAG, "Retrying signaling MQTT");
    esp_mqtt_client_reconnect(sig->client);
}

static void free_ice_info(esp_peer_signaling_ice_info_t *info)
{
    free(info->server_info.stun_url);
    free(info->server_info.user);
    free(info->server_info.psw);
    memset(info, 0, sizeof(*info));
}

/* strdup that maps "" to NULL, so an unset TURN credential reaches esp_peer
 * as absent rather than as an empty username. */
static char *dup_or_null(const char *s)
{
    return (s != NULL && s[0] != '\0') ? strdup(s) : NULL;
}

static int publish_json(mqtt_sig_t *sig, cJSON *json, bool retain)
{
    char *payload = cJSON_PrintUnformatted(json);
    if (payload == NULL) {
        return -1;
    }
    int msg_id = esp_mqtt_client_publish(sig->client, sig->pub_topic, payload,
                                         0, 1, retain ? 1 : 0);
    ESP_LOGD(TAG, "-> %s : %s", sig->pub_topic, payload);
    free(payload);
    return msg_id < 0 ? -1 : 0;
}

/* SDP candidate address is IPv6 if the 5th field contains ':'. */
static bool ice_candidate_is_ipv6(const char *cand)
{
    const char *p = cand;
    if (strncmp(p, "candidate:", 10) == 0) {
        p += 10;
    }
    for (int i = 0; i < 4; i++) {
        p = strchr(p, ' ');
        if (p == NULL) {
            return false;
        }
        p++;
    }
    const char *end = strchr(p, ' ');
    if (end == NULL) {
        end = p + strlen(p);
    }
    for (const char *q = p; q < end; q++) {
        if (*q == ':') {
            return true;
        }
    }
    return false;
}

/* Translates one inbound JSON object into the esp_peer_signaling_msg_t that
 * esp_webrtc expects. Buffers handed up are NUL-terminated: esp_peer's own
 * customized-data path reads data[size] to validate the string. */
static void dispatch_message(mqtt_sig_t *sig, const char *text, int len)
{
    if (sig->cfg.on_msg == NULL) {
        return;
    }
    cJSON *root = cJSON_ParseWithLength(text, (size_t)len);
    if (root == NULL) {
        ESP_LOGE(TAG, "Discarding malformed signaling payload");
        return;
    }

    const cJSON *type = cJSON_GetObjectItem(root, "type");
    if (!cJSON_IsString(type)) {
        ESP_LOGE(TAG, "Signaling payload has no type");
        cJSON_Delete(root);
        return;
    }
    ESP_LOGI(TAG, "<- %s", type->valuestring);

    esp_peer_signaling_msg_t msg = { .type = ESP_PEER_SIGNALING_MSG_NONE };
    const cJSON *field = NULL;

    if (strcmp(type->valuestring, "offer") == 0 ||
        strcmp(type->valuestring, "answer") == 0) {
        field = cJSON_GetObjectItem(root, "sdp");
        msg.type = ESP_PEER_SIGNALING_MSG_SDP;

        /* The last moment at which the audio hardware can still be taken
         * off the sentinel.
         *
         * Handing this offer up runs esp_webrtc's pc_start(), which sets
         * up its capture sink and then enables it — and enabling it opens
         * the same ES7210 the sentinel holds. Whatever this costs in
         * answer latency is paid once per call; getting the order wrong
         * costs the call its microphone for its whole duration, silently.
         *
         * Blocking the signaling task here is safe: it has no other work
         * in flight, and the candidates that follow are queued by the
         * broker rather than lost. */
        if (strcmp(type->valuestring, "offer") == 0) {
            mode_arbiter_enter_call();
        }
    } else if (strcmp(type->valuestring, "candidate") == 0) {
        field = cJSON_GetObjectItem(root, "candidate");
        msg.type = ESP_PEER_SIGNALING_MSG_CANDIDATE;
    } else if (strcmp(type->valuestring, "customized") == 0) {
        field = cJSON_GetObjectItem(root, "data");
        msg.type = ESP_PEER_SIGNALING_MSG_CUSTOMIZED;
    } else if (strcmp(type->valuestring, "bye") == 0) {
        msg.type = ESP_PEER_SIGNALING_MSG_BYE;
        sig->cfg.on_msg(&msg, sig->cfg.ctx);
        cJSON_Delete(root);
        return;
    } else {
        ESP_LOGW(TAG, "Ignoring unknown signaling type '%s'",
                 type->valuestring);
        cJSON_Delete(root);
        return;
    }

    if (!cJSON_IsString(field)) {
        ESP_LOGE(TAG, "Signaling '%s' is missing its payload field",
                 type->valuestring);
        cJSON_Delete(root);
        return;
    }
    /* Drop IPv6 trickle candidates: they burn ICE slots and add SDIO load
     * on the C6 path; same-LAN calls only need the IPv4 host candidate. */
    if (msg.type == ESP_PEER_SIGNALING_MSG_CANDIDATE &&
        ice_candidate_is_ipv6(field->valuestring)) {
        ESP_LOGD(TAG, "Dropping IPv6 candidate");
        cJSON_Delete(root);
        return;
    }
    msg.data = (uint8_t *)field->valuestring;
    msg.size = (int)strlen(field->valuestring);
    sig->cfg.on_msg(&msg, sig->cfg.ctx);
    cJSON_Delete(root);
}

static void handle_data_event(mqtt_sig_t *sig, esp_mqtt_event_handle_t event)
{
    /* Single-fragment message: the common case, dispatched without copying. */
    if (event->current_data_offset == 0 &&
        event->data_len == event->total_data_len) {
        dispatch_message(sig, event->data, event->data_len);
        return;
    }

    if (event->current_data_offset == 0) {
        free(sig->rx_buf);
        sig->rx_buf = malloc((size_t)event->total_data_len + 1);
        sig->rx_len = 0;
        sig->rx_expected = event->total_data_len;
        if (sig->rx_buf == NULL) {
            ESP_LOGE(TAG, "No memory to reassemble a %d byte payload",
                     event->total_data_len);
            sig->rx_expected = 0;
            return;
        }
    }
    if (sig->rx_buf == NULL || sig->rx_len + event->data_len > sig->rx_expected) {
        /* A fragment arrived without its head, or the total grew. Either way
         * the message is unrecoverable; drop it rather than dispatch a
         * truncated SDP. */
        free(sig->rx_buf);
        sig->rx_buf = NULL;
        sig->rx_expected = 0;
        return;
    }

    memcpy(sig->rx_buf + sig->rx_len, event->data, event->data_len);
    sig->rx_len += event->data_len;
    if (sig->rx_len == sig->rx_expected) {
        sig->rx_buf[sig->rx_len] = '\0';
        dispatch_message(sig, sig->rx_buf, sig->rx_len);
        free(sig->rx_buf);
        sig->rx_buf = NULL;
        sig->rx_len = 0;
        sig->rx_expected = 0;
    }
}

/* ── presence ──────────────────────────────────────────────────────────
 *
 * Three retained publishes make this unit discoverable:
 *
 *   devices/<id>/status        {"status":"online"}
 *   devices/<id>/ip            192.168.2.10
 *   devices/<id>/capabilities  {"WebRTC":true,"Camera":true,...}
 *
 * Retained, so the dashboard learns about the unit the moment it subscribes
 * rather than having to wait for the next beat -- which matters because the
 * server resets every status to offline on startup.
 *
 * The status publish then repeats on a timer. The client's single Last Will
 * is already spent on the call-teardown bye, so a crash cannot be announced
 * directly; the server's five-minute staleness rule covers that instead, and
 * the heartbeat is what keeps it satisfied. */

static void presence_publish_ip(mqtt_sig_t *sig)
{
    esp_netif_t *netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    if (netif == NULL) {
        netif = esp_netif_get_handle_from_ifkey("ETH_DEF");
    }
    esp_netif_ip_info_t info;
    if (netif == NULL || esp_netif_get_ip_info(netif, &info) != ESP_OK ||
        info.ip.addr == 0) {
        return;
    }
    char ip[16];
    snprintf(ip, sizeof(ip), IPSTR, IP2STR(&info.ip));
    esp_mqtt_client_publish(sig->client, sig->ip_topic, ip, 0, 1, true);
}

static void presence_announce(mqtt_sig_t *sig)
{
    if (sig == NULL || sig->client == NULL || !sig->connected) {
        return;
    }
    esp_mqtt_client_publish(sig->client, sig->status_topic,
                            MQTT_STATUS_PAYLOAD_ONLINE, 0, 1, true);
    presence_publish_ip(sig);
    esp_mqtt_client_publish(sig->client, sig->caps_topic,
                            "{\"WebRTC\":true,\"Camera\":true,"
                            "\"duplex\":true,\"video\":true}",
                            0, 1, true);
    ESP_LOGI(TAG, "presence: %s -> online", sig->status_topic);
}

static void presence_heartbeat_cb(void *arg)
{
    mqtt_sig_t *sig = (mqtt_sig_t *)arg;
    if (sig == NULL || !sig->connected) {
        return;
    }
    esp_mqtt_client_publish(sig->client, sig->status_topic,
                            MQTT_STATUS_PAYLOAD_ONLINE, 0, 1, true);
}

static void presence_start(mqtt_sig_t *sig)
{
    presence_announce(sig);
    if (sig->heartbeat != NULL) {
        return;                       /* already ticking across a reconnect */
    }
    const esp_timer_create_args_t args = {
        .callback = presence_heartbeat_cb,
        .arg = sig,
        .name = "p4_presence",
    };
    if (esp_timer_create(&args, &sig->heartbeat) == ESP_OK) {
        esp_timer_start_periodic(sig->heartbeat, PRESENCE_HEARTBEAT_US);
    } else {
        ESP_LOGW(TAG, "no timer for presence heartbeat");
    }
}

static void presence_stop(mqtt_sig_t *sig)
{
    if (sig->heartbeat != NULL) {
        esp_timer_stop(sig->heartbeat);
        esp_timer_delete(sig->heartbeat);
        sig->heartbeat = NULL;
    }
}

static void mqtt_event_handler(void *handler_args, esp_event_base_t base,
                               int32_t event_id, void *event_data)
{
    mqtt_sig_t *sig = (mqtt_sig_t *)handler_args;
    esp_mqtt_event_handle_t event = (esp_mqtt_event_handle_t)event_data;

    switch ((esp_mqtt_event_id_t)event_id) {
    case MQTT_EVENT_CONNECTED:
        ESP_LOGI(TAG, "Broker connected, subscribing to %s", sig->sub_topic);
        if (sig->tls_name[0] != '\0') {
            broker_addr_remember(sig->tls_name);
        }
        esp_mqtt_client_subscribe(sig->client, sig->sub_topic, 1);
        sig->connected = true;
        /* Announce before the peer callbacks below: those can take a while
         * gathering ICE, and the dashboard should not have to wait for a
         * call to find out this unit exists. */
        presence_start(sig);
        /* ICE information has to reach esp_peer before it is told the
         * signaling channel is up, otherwise the first offer is gathered
         * with no STUN server and only host candidates get advertised. */
        if (sig->cfg.on_ice_info) {
            sig->cfg.on_ice_info(&sig->ice_info, sig->cfg.ctx);
        }
        if (sig->cfg.on_connected) {
            sig->cfg.on_connected(sig->cfg.ctx);
        }
        break;

    case MQTT_EVENT_DISCONNECTED:
        sig->connected = false;
        if (sig->stopping) {
            ESP_LOGI(TAG, "Broker disconnected (signaling stopping)");
            break;
        }
        /* Immediate reconnect races HTTP ingest and the other MQTT client
         * for the single lwIP getaddrinfo slot (MEMP_NUM_NETDB=1). That is
         * EAI_FAIL 202, after which snippets print "No MQTT!". Wait, then
         * retry; the call's audio/video does not use this socket. */
        ESP_LOGW(TAG, "Broker disconnected; signaling reconnect in 8 s");
        if (sig->reconnect_timer) {
            esp_timer_stop(sig->reconnect_timer);
            esp_timer_start_once(sig->reconnect_timer, 8 * 1000000ULL);
        }
        break;

    case MQTT_EVENT_DATA:
        handle_data_event(sig, event);
        break;

    case MQTT_EVENT_ERROR:
        ESP_LOGE(TAG, "MQTT transport error");
        break;

    default:
        break;
    }
}

static int mqtt_signal_stop(esp_peer_signaling_handle_t h);

static int mqtt_signal_start(esp_peer_signaling_cfg_t *cfg,
                             esp_peer_signaling_handle_t *h)
{
    if (cfg == NULL || h == NULL || cfg->extra_cfg == NULL ||
        cfg->extra_size != sizeof(mqtt_signaling_cfg_t)) {
        return -1;
    }
    const mqtt_signaling_cfg_t *mc = (const mqtt_signaling_cfg_t *)cfg->extra_cfg;
    if (mc->broker_uri == NULL || mc->room == NULL) {
        ESP_LOGE(TAG, "Broker URI and room are both required");
        return -1;
    }

    mqtt_sig_t *sig = calloc(1, sizeof(mqtt_sig_t));
    if (sig == NULL) {
        return -1;
    }
    sig->cfg = *cfg;
    snprintf(sig->sub_topic, sizeof(sig->sub_topic), "%s/%s/callee",
             MQTT_SIGNAL_TOPIC_PREFIX, mc->room);
    snprintf(sig->pub_topic, sizeof(sig->pub_topic), "%s/%s/caller",
             MQTT_SIGNAL_TOPIC_PREFIX, mc->room);

    /* The room name is this unit's device id, so presence lands on the same
     * identifier the dashboard already lists and calls. */
    snprintf(sig->status_topic, sizeof(sig->status_topic), "%s/%s/status",
             MQTT_DEVICE_TOPIC_BASE, mc->room);
    snprintf(sig->ip_topic, sizeof(sig->ip_topic), "%s/%s/ip",
             MQTT_DEVICE_TOPIC_BASE, mc->room);
    snprintf(sig->caps_topic, sizeof(sig->caps_topic), "%s/%s/capabilities",
             MQTT_DEVICE_TOPIC_BASE, mc->room);

    /* This unit answers; it never offers. Reporting is_initiator = false
     * makes esp_peer take the controlled ICE role and wait for the offer. */
    sig->ice_info.is_initiator = false;
    sig->ice_info.server_info.stun_url = dup_or_null(mc->stun_url);
    sig->ice_info.server_info.user = dup_or_null(mc->turn_user);
    sig->ice_info.server_info.psw = dup_or_null(mc->turn_password);

    uint16_t broker_port = 1883;
    bool broker_ssl = false;
    bool parsed = broker_addr_parse_mqtt_uri(mc->broker_uri, sig->tls_name,
                                             sizeof(sig->tls_name),
                                             &broker_port, &broker_ssl);
    if (parsed) {
        broker_addr_remember(sig->tls_name);
        broker_addr_connect_host(sig->tls_name, sig->connect_host,
                                 sizeof(sig->connect_host));
        ESP_LOGI(TAG, "Broker %s via %s:%u", sig->tls_name,
                 sig->connect_host, broker_port);
    } else {
        sig->tls_name[0] = '\0';
        sig->connect_host[0] = '\0';
        broker_ssl = strncmp(mc->broker_uri, "mqtts://", 8) == 0;
    }

    esp_mqtt_client_config_t mqtt_cfg = {
        .credentials.username = (mc->username && mc->username[0])
                                    ? mc->username : NULL,
        .credentials.authentication.password = (mc->password && mc->password[0])
                                    ? mc->password : NULL,
        /* If this unit falls off the network mid-call the broker publishes
         * the bye on its behalf, so the caller stops sending immediately
         * instead of waiting for an ICE timeout. */
        .session.last_will.topic = sig->pub_topic,
        .session.last_will.msg = "{\"type\":\"bye\"}",
        .session.last_will.msg_len = 14,
        .session.last_will.qos = 1,
        .session.last_will.retain = 0,
        /* 90 s: a 30 s call plus SDIO congestion was enough for the broker
         * to miss a ping, which used to hang up the call and take snippets
         * down with it. Media does not ride this socket. */
        .session.keepalive = 90,
        /* Reconnect is driven from MQTT_EVENT_DISCONNECTED so a stop() can
         * suppress it; the default auto-reconnect would race destroy(). */
        .network.disable_auto_reconnect = true,
    };
    if (parsed) {
        mqtt_cfg.broker.address.hostname = sig->connect_host;
        mqtt_cfg.broker.address.port = broker_port;
        mqtt_cfg.broker.address.transport = broker_ssl
                                                ? MQTT_TRANSPORT_OVER_SSL
                                                : MQTT_TRANSPORT_OVER_TCP;
        if (broker_ssl) {
            mqtt_cfg.broker.verification.common_name = sig->tls_name;
        }
    } else {
        mqtt_cfg.broker.address.uri = mc->broker_uri;
    }

    /* Mutual TLS. The broker's LAN listener sets require_certificate and
     * ignores the username entirely, so the client certificate is what
     * actually authenticates this unit; the username above only matters on a
     * plain mqtt:// broker. Lengths stay zero because the certificates are
     * embedded with EMBED_TXTFILES, which NUL-terminates them. */
    if (broker_ssl) {
        if (mc->ca_cert == NULL || mc->client_cert == NULL ||
            mc->client_key == NULL) {
            ESP_LOGE(TAG, "mqtts:// needs a CA, a certificate and a key");
            free_ice_info(&sig->ice_info);
            free(sig);
            return -1;
        }
        mqtt_cfg.broker.verification.certificate = mc->ca_cert;
        mqtt_cfg.credentials.authentication.certificate = mc->client_cert;
        mqtt_cfg.credentials.authentication.key = mc->client_key;
    }

    sig->client = esp_mqtt_client_init(&mqtt_cfg);
    if (sig->client == NULL) {
        ESP_LOGE(TAG, "Failed to create MQTT client");
        free_ice_info(&sig->ice_info);
        free(sig);
        return -1;
    }
    const esp_timer_create_args_t reconn_args = {
        .callback = reconnect_timer_cb,
        .arg = sig,
        .dispatch_method = ESP_TIMER_TASK,
        .name = "mqtt_sig_re",
    };
    if (esp_timer_create(&reconn_args, &sig->reconnect_timer) != ESP_OK) {
        ESP_LOGE(TAG, "Failed to create signaling reconnect timer");
        *h = NULL;
        mqtt_signal_stop(sig);
        return -1;
    }
    esp_mqtt_client_register_event(sig->client, ESP_EVENT_ANY_ID,
                                   mqtt_event_handler, sig);
    if (esp_mqtt_client_start(sig->client) != ESP_OK) {
        ESP_LOGE(TAG, "Failed to connect to %s", mc->broker_uri);
        *h = NULL;
        mqtt_signal_stop(sig);
        return -1;
    }

    ESP_LOGI(TAG, "Signaling on %s (%s:%u) as callee in room %s",
             mc->broker_uri,
             sig->connect_host[0] ? sig->connect_host : "uri",
             (unsigned)broker_port, mc->room);
    *h = sig;
    return 0;
}

static int mqtt_signal_send_msg(esp_peer_signaling_handle_t h,
                                esp_peer_signaling_msg_t *msg)
{
    mqtt_sig_t *sig = (mqtt_sig_t *)h;
    if (sig == NULL || msg == NULL) {
        return -1;
    }
    if (!sig->connected) {
        ESP_LOGW(TAG, "Dropping outbound message, broker not connected");
        return -1;
    }

    cJSON *json = cJSON_CreateObject();
    if (json == NULL) {
        return -1;
    }
    int ret = 0;
    switch (msg->type) {
    case ESP_PEER_SIGNALING_MSG_SDP:
        /* Always an answer: the caller offers, this unit responds. */
        cJSON_AddStringToObject(json, "type", "answer");
        cJSON_AddStringToObject(json, "sdp", (char *)msg->data);
        break;
    case ESP_PEER_SIGNALING_MSG_CANDIDATE:
        cJSON_AddStringToObject(json, "type", "candidate");
        cJSON_AddStringToObject(json, "candidate", (char *)msg->data);
        break;
    case ESP_PEER_SIGNALING_MSG_CUSTOMIZED:
        cJSON_AddStringToObject(json, "type", "customized");
        cJSON_AddStringToObject(json, "data", (char *)msg->data);
        break;
    case ESP_PEER_SIGNALING_MSG_BYE:
        cJSON_AddStringToObject(json, "type", "bye");
        break;
    default:
        cJSON_Delete(json);
        return 0;
    }
    ret = publish_json(sig, json, false);
    cJSON_Delete(json);
    return ret;
}

static int mqtt_signal_stop(esp_peer_signaling_handle_t h)
{
    mqtt_sig_t *sig = (mqtt_sig_t *)h;
    if (sig == NULL) {
        return -1;
    }
    sig->stopping = true;
    if (sig->reconnect_timer) {
        esp_timer_stop(sig->reconnect_timer);
        esp_timer_delete(sig->reconnect_timer);
        sig->reconnect_timer = NULL;
    }
    presence_stop(sig);
    if (sig->client) {
        if (sig->connected) {
            esp_peer_signaling_msg_t bye = {
                .type = ESP_PEER_SIGNALING_MSG_BYE,
            };
            mqtt_signal_send_msg(sig, &bye);
            /* Going away deliberately, so say so rather than leaving the
             * dashboard to time this unit out five minutes later. */
            esp_mqtt_client_publish(sig->client, sig->status_topic,
                                    MQTT_STATUS_PAYLOAD_OFFLINE, 0, 1, true);
        }
        esp_mqtt_client_stop(sig->client);
        esp_mqtt_client_destroy(sig->client);
        sig->client = NULL;
    }
    /* Peer teardown belongs to an intentional session close, not a
     * keepalive miss. */
    if (sig->cfg.on_close) {
        sig->cfg.on_close(sig->cfg.ctx);
    }
    free_ice_info(&sig->ice_info);
    free(sig->rx_buf);
    free(sig);
    return 0;
}

const esp_peer_signaling_impl_t *esp_signaling_get_mqtt_impl(void)
{
    static const esp_peer_signaling_impl_t impl = {
        .start = mqtt_signal_start,
        .send_msg = mqtt_signal_send_msg,
        .stop = mqtt_signal_stop,
    };
    return &impl;
}
