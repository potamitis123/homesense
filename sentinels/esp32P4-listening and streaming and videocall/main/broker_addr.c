#include "broker_addr.h"

#include <arpa/inet.h>
#include <netdb.h>
#include <string.h>
#include <stdlib.h>

#include "esp_log.h"

static const char *TAG = "BROKER_ADDR";

static char s_host[128];
static char s_ip[16];
static volatile bool s_ok;

static bool host_is_ipv4(const char *host)
{
    struct in_addr numeric;
    return host != NULL && inet_aton(host, &numeric) == 1;
}

static bool resolve_ipv4(const char *host, char *ip, size_t ip_len)
{
    if (host == NULL || ip == NULL || ip_len < 8) {
        return false;
    }
    ip[0] = '\0';

    struct in_addr numeric;
    if (inet_aton(host, &numeric) == 1) {
        return inet_ntop(AF_INET, &numeric, ip, ip_len) != NULL;
    }

    struct addrinfo hints = {
        .ai_family = AF_INET,
        .ai_socktype = SOCK_STREAM,
    };
    struct addrinfo *res = NULL;
    int err = getaddrinfo(host, NULL, &hints, &res);
    if (err != 0 || res == NULL || res->ai_addr == NULL) {
        ESP_LOGW(TAG, "getaddrinfo(%s) returned %d", host, err);
        if (res != NULL) {
            freeaddrinfo(res);
        }
        return false;
    }
    const struct sockaddr_in *sa = (const struct sockaddr_in *)res->ai_addr;
    const char *ok = inet_ntop(AF_INET, &sa->sin_addr, ip, ip_len);
    freeaddrinfo(res);
    return ok != NULL && ip[0] != '\0';
}

bool broker_addr_parse_mqtt_uri(const char *uri, char *host, size_t host_len,
                                uint16_t *port, bool *ssl)
{
    if (uri == NULL || host == NULL || port == NULL || ssl == NULL) {
        return false;
    }
    const char *p = uri;
    *ssl = false;
    *port = 1883;
    if (strncmp(p, "mqtts://", 8) == 0) {
        *ssl = true;
        *port = 8883;
        p += 8;
    } else if (strncmp(p, "mqtt://", 7) == 0) {
        p += 7;
    } else {
        return false;
    }
    if (*p == '[') {
        return false;
    }
    const char *slash = strchr(p, '/');
    const char *colon = strchr(p, ':');
    const char *end = slash != NULL ? slash : p + strlen(p);
    if (colon != NULL && colon < end) {
        size_t n = (size_t)(colon - p);
        if (n == 0 || n >= host_len) {
            return false;
        }
        memcpy(host, p, n);
        host[n] = '\0';
        int parsed = atoi(colon + 1);
        if (parsed <= 0 || parsed > 65535) {
            return false;
        }
        *port = (uint16_t)parsed;
        return true;
    }
    size_t n = (size_t)(end - p);
    if (n == 0 || n >= host_len) {
        return false;
    }
    memcpy(host, p, n);
    host[n] = '\0';
    return true;
}

bool broker_addr_lookup(const char *host, char *ip, size_t ip_len)
{
    if (!s_ok || host == NULL || ip == NULL || strcmp(s_host, host) != 0) {
        return false;
    }
    strlcpy(ip, s_ip, ip_len);
    return ip[0] != '\0';
}

bool broker_addr_remember(const char *host)
{
    if (host == NULL || host[0] == '\0') {
        return false;
    }
    if (s_ok && strcmp(s_host, host) == 0) {
        return true;
    }
    char ip[16];
    if (!resolve_ipv4(host, ip, sizeof(ip))) {
        return false;
    }
    strlcpy(s_host, host, sizeof(s_host));
    strlcpy(s_ip, ip, sizeof(s_ip));
    s_ok = true;
    ESP_LOGI(TAG, "Cached %s -> %s", host, ip);
    return true;
}

bool broker_addr_connect_host(const char *host, char *buf, size_t buf_len)
{
    if (host == NULL || buf == NULL || buf_len == 0) {
        return false;
    }
    if (broker_addr_lookup(host, buf, buf_len)) {
        return true;
    }
    strlcpy(buf, host, buf_len);
    return host_is_ipv4(buf);
}
