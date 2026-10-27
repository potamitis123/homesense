#!/usr/bin/env python3
"""
dns_redirector.py
-----------------
A lightweight, zero-dependency DNS Server / Proxy that runs locally.
- Automatically resolves 'audiosense.hmu.gr' to this laptop's local IP address.
- Forwards all other DNS requests to an upstream public DNS server (e.g., 8.8.8.8).

Usage:
  1. Identify your laptop's local IP address (e.g., 192.168.2.12).
  2. Run this script as Administrator/Root (since port 53 is privileged).
     python dns_redirector.py
  3. Configure your ESP32 client's DNS setting (or your router's DHCP DNS setting)
     to point to your laptop's local IP address.
"""

import socket
import sys
import threading

# --- Configuration ---
TARGET_DOMAIN = "audiosense.hmu.gr"
UPSTREAM_DNS = "8.8.8.8"
DNS_PORT = 53

def get_local_ip():
    """Dynamically find the primary local IP address of this machine."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Doesn't need to be reachable, just triggers local routing table lookup
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip

LOCAL_IP = get_local_ip()

def parse_dns_question(data):
    """Parses the DNS query question section to extract the requested domain name."""
    try:
        # Header is 12 bytes
        i = 12
        parts = []
        while True:
            length = data[i]
            if length == 0:
                break
            i += 1
            parts.append(data[i:i+length].decode('utf-8', errors='ignore'))
            i += length
        domain = ".".join(parts)
        # i + 5 covers the trailing 0 byte, Type (2 bytes), and Class (2 bytes)
        return domain, i + 5
    except Exception:
        return None, None

def build_dns_response(query_packet, question_end, target_ip):
    """Builds a DNS A-record response pointing to the target local IP."""
    # Header: Transaction ID (same as query), Flags: standard query response, no error
    header = query_packet[:2] + b"\x81\x80"
    header += b"\x00\x01"  # Questions: 1
    header += b"\x00\x01"  # Answer RRs: 1
    header += b"\x00\x00"  # Authority RRs: 0
    header += b"\x00\x00"  # Additional RRs: 0

    # Question Section (copied verbatim from query)
    question = query_packet[12:question_end]

    # Answer Section
    answer = b"\xc0\x0c"          # Compression pointer to the domain name in the question
    answer += b"\x00\x01"         # Type: A (IPv4)
    answer += b"\x00\x01"         # Class: IN
    answer += b"\x00\x00\x01\x2c" # TTL: 300 seconds
    answer += b"\x00\x04"         # Data Length: 4 bytes
    answer += bytes(map(int, target_ip.split('.'))) # IP Address bytes

    return header + question + answer

def handle_query(data, addr, server_socket):
    """Processes a single DNS query."""
    domain, question_end = parse_dns_question(data)
    
    if domain and domain.lower() == TARGET_DOMAIN.lower():
        print(f"[*] DNS Query: {domain} from {addr[0]} -> Redirection to Local IP: {LOCAL_IP}")
        response = build_dns_response(data, question_end, LOCAL_IP)
        server_socket.sendto(response, addr)
    else:
        # Forward to upstream public DNS
        if domain:
            print(f"[.] Forwarding Query: {domain} from {addr[0]} -> Upstream ({UPSTREAM_DNS})")
        
        forward_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        forward_socket.settimeout(3.0)
        try:
            forward_socket.sendto(data, (UPSTREAM_DNS, DNS_PORT))
            resp, _ = forward_socket.recvfrom(2048)
            server_socket.sendto(resp, addr)
        except socket.timeout:
            print(f"[!] Upstream DNS timeout for query: {domain}")
        except Exception as e:
            print(f"[!] Upstream error: {e}")
        finally:
            forward_socket.close()

def main():
    print("=====================================================================")
    print("               Sentinel Local DNS Redirection Server                 ")
    print("=====================================================================")
    print(f"Target Redirection Domain : {TARGET_DOMAIN}")
    print(f"Resolving to Local IP    : {LOCAL_IP}")
    print(f"Upstream DNS Server      : {UPSTREAM_DNS}")
    print("=====================================================================")

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        server_socket.bind(('0.0.0.0', DNS_PORT))
    except PermissionError:
        print("[!] ERROR: Permission Denied. You must run this script with administrative / sudo privileges.")
        print("[!] On Windows, run PowerShell/CMD as Administrator.")
        sys.exit(1)
    except Exception as e:
        print(f"[!] Bind Error on port 53: {e}")
        sys.exit(1)

    print(f"[*] Listening for DNS requests on port {DNS_PORT}...")
    
    while True:
        try:
            data, addr = server_socket.recvfrom(2048)
            t = threading.Thread(target=handle_query, args=(data, addr, server_socket))
            t.daemon = True
            t.start()
        except KeyboardInterrupt:
            print("\n[*] Shutting down local DNS server.")
            break
        except Exception as e:
            print(f"[!] Socket loop error: {e}")

    server_socket.close()

if __name__ == "__main__":
    main()
