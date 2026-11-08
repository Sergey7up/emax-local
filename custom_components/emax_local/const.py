"""Constants for the passive EMAX receiver."""

DOMAIN = "emax_local"

CONF_STATION_IP = "station_ip"
CONF_RECEIVER_IP = "receiver_ip"
CONF_SETUP_REQUEST = "setup_request"
CONF_LAST_SETUP = "last_setup"
CONF_EXPECTED_MAC = "expected_mac"
CONF_LISTEN_PORT = "listen_port"

DEFAULT_LISTEN_PORT = 10000
DEFAULT_NAME = "EMAX Local"

STALE_AFTER_SECONDS = 180
MAX_CLIENTS = 8
CLIENT_IDLE_SECONDS = 600

CHANNELS = ("indoor", "ch1", "ch2", "ch3")
