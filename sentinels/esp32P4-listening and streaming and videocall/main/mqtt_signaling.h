/* MQTT signaling for esp_webrtc.
 *
 * Drop-in replacement for esp_signaling_get_apprtc_impl(): it satisfies the
 * same esp_peer_signaling_impl_t contract, so esp_webrtc itself is untouched.
 *
 * Topic layout, with `room` taken from mqtt_signaling_cfg_t (normally the
 * device id) and `prefix` from MQTT_SIGNAL_TOPIC_PREFIX:
 *
 *     <prefix>/<room>/callee   messages addressed to this unit  (subscribed)
 *     <prefix>/<room>/caller   messages addressed to the peer   (published)
 *
 * The split is what lets both sides use one broker with no client-id
 * negotiation and no initiator election: this unit is always the callee, so
 * it always answers and never offers. That is also why on_ice_info reports
 * is_initiator = false unconditionally.
 *
 * Payloads are single JSON objects, matching the inner message schema
 * apprtc uses so that a browser peer needs no translation layer:
 *
 *     {"type":"offer",      "sdp":"v=0\r\n..."}
 *     {"type":"answer",     "sdp":"v=0\r\n..."}
 *     {"type":"candidate",  "candidate":"candidate:..."}
 *     {"type":"bye"}
 *     {"type":"customized", "data":"RING"}
 *
 * A retained `bye` is registered as the MQTT last will, so a caller that
 * drops off the network tears the call down instead of leaving this unit
 * streaming to nobody.
 */

#pragma once

#include "esp_peer_signaling.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Broker and ICE settings, passed through esp_webrtc's signaling
 *        extra_cfg
 *
 * @note All strings are copied during start(), so the caller may free them
 *       once esp_webrtc_open() returns. The three PEM fields are the
 *       exception: esp-mqtt keeps the pointers, so they must outlive the
 *       client. Passing the linker-embedded certificates satisfies that.
 */
typedef struct {
    const char *broker_uri;    /*!< e.g. "mqtts://your-broker.example.com:8883" */
    const char *username;      /*!< Broker user, NULL or "" if anonymous */
    const char *password;      /*!< Broker password, NULL or "" if none */
    const char *room;          /*!< Topic namespace, normally the device id */
    const char *stun_url;      /*!< STUN/TURN URL handed to esp_peer */
    const char *turn_user;     /*!< TURN user, NULL or "" for plain STUN */
    const char *turn_password; /*!< TURN credential */

    /* Required when broker_uri is mqtts://. The home-server broker sets
     * require_certificate, so a client certificate is not optional there:
     * without one the TLS handshake is refused and no call can be signaled. */
    const char *ca_cert;       /*!< CA that signed the broker, PEM, NUL-terminated */
    const char *client_cert;   /*!< This unit's certificate, PEM */
    const char *client_key;    /*!< This unit's private key, PEM */
} mqtt_signaling_cfg_t;

/**
 * @brief Signaling implementation to hand to esp_webrtc_cfg_t.signaling_impl
 */
const esp_peer_signaling_impl_t *esp_signaling_get_mqtt_impl(void);

#ifdef __cplusplus
}
#endif
