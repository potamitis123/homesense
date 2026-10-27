/* Cached IPv4 for the MQTT broker.
 *
 * lwIP on this target has one NETDB slot (MEMP_NUM_NETDB=1). A second
 * getaddrinfo while the first is in flight returns 202 (EAI_FAIL) and
 * DNS stays wedged until reboot — that is the "No MQTT!" after a call.
 *
 * Resolve once while the link is quiet, then connect by the dotted IP.
 * TLS still uses the original hostname for SNI and certificate CN
 * (esp_mqtt_client_config_t.broker.verification.common_name).
 */

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

bool broker_addr_parse_mqtt_uri(const char *uri, char *host, size_t host_len,
                                uint16_t *port, bool *ssl);

/* Resolve host to IPv4 and cache it. Returns true once a dotted IP is
 * available for this host. Safe to call again; a cache hit does no DNS. */
bool broker_addr_remember(const char *host);

/* Copy the cached IPv4 for host. False if this host has not been resolved. */
bool broker_addr_lookup(const char *host, char *ip, size_t ip_len);

/* Address MQTT should dial: cached IP if present, otherwise host.
 * Returns true when buf is a dotted IPv4. */
bool broker_addr_connect_host(const char *host, char *buf, size_t buf_len);

#ifdef __cplusplus
}
#endif
