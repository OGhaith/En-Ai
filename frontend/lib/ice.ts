// Leave iceServers unset so livekit-client accepts the short-lived TURN
// credentials returned by the local LiveKit server during JoinResponse.
// The server relays media over TLS/TCP on 192.168.197.45:443 when the browser
// is on another subnet and cannot use a direct UDP candidate.
export const APP_ICE_SERVERS: RTCIceServer[] = [];

export const APP_RTC_CONFIG: RTCConfiguration = {
  iceTransportPolicy: 'all',
};
