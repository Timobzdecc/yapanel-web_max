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
        echo "nameserver 192.168.1.1" > /data/etc/resolv.conf
        echo "nameserver 8.8.8.8" >> /data/etc/resolv.conf
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
exec /data/python/bin/python3 server.py --host 0.0.0.0 --port 8765 --device local --model max >> /data/yapanel/yapanel.log 2>&1
