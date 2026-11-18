#!/system/bin/sh
# Ensure musl runtime dynamic linker is available in /lib
if [ ! -f /lib/ld-musl-aarch64.so.1 ]; then
    mkdir -p /data/lib
    mount -o bind /data/lib /lib
fi

# Ensure DNS resolution for musl libc
if [ ! -f /system/etc/resolv.conf ]; then
    mkdir -p /data/etc
    if [ ! -f /data/etc/resolv.conf ]; then
        cp -a /system/etc/. /data/etc/ 2>/dev/null
        : > /data/etc/resolv.conf
        for p in net.dns1 net.dns2; do
            d=$(getprop "$p" 2>/dev/null)
            case "$d" in
                ""|127.*|::1) ;;
                *) grep -qxF "nameserver $d" /data/etc/resolv.conf 2>/dev/null || echo "nameserver $d" >> /data/etc/resolv.conf ;;
            esac
        done
        if [ ! -s /data/etc/resolv.conf ]; then
            gw=$(ip route get 8.8.8.8 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="via"){print $(i+1); exit}}')
            [ -n "$gw" ] && echo "nameserver $gw" >> /data/etc/resolv.conf
        fi
        echo "nameserver 8.8.8.8" >> /data/etc/resolv.conf
        echo "nameserver 1.1.1.1" >> /data/etc/resolv.conf
        chmod 644 /data/etc/resolv.conf
    fi
    mount -o bind /data/etc /system/etc
fi

# Ensure consolidated root CA bundle for OpenSSL / Python ssl
if [ ! -f /data/etc/ssl/cert.pem ] || [ ! -s /data/etc/ssl/cert.pem ]; then
    mkdir -p /data/etc/ssl/certs
    cat /system/etc/security/cacerts/*.0 > /data/etc/ssl/cert.pem 2>/dev/null
    cp /data/etc/ssl/cert.pem /data/etc/ssl/certs/ca-certificates.crt
    chmod 644 /data/etc/ssl/cert.pem
fi

export SSL_CERT_FILE=/data/etc/ssl/cert.pem
export REQUESTS_CA_BUNDLE=/data/etc/ssl/cert.pem
export PATH=/data/python/bin:/system/bin:/system/xbin:/vendor/bin:/odm/bin:$PATH
export PYTHONHOME=/data/python
export PANEL_LOCAL=1

cd /data/yapanel
if (ps -A -o ARGS 2>/dev/null || ps 2>/dev/null) | grep '[s]erver\.py --host' >/dev/null 2>&1; then
    echo "run.sh: server.py --host already running, skipping duplicate launch" >> /data/yapanel/yapanel.log
    exit 0
fi
if [ -f /data/yapanel/yapanel.log ]; then
    log_sz=$(wc -c < /data/yapanel/yapanel.log 2>/dev/null | tr -dc '0-9')
    if [ -n "$log_sz" ] && [ "$log_sz" -gt 524288 ] 2>/dev/null; then
        mv -f /data/yapanel/yapanel.log /data/yapanel/yapanel.log.1 2>/dev/null || true
    fi
fi
exec /data/python/bin/python3 server.py --host 0.0.0.0 --port 8765 --device local --model max >> /data/yapanel/yapanel.log 2>&1
