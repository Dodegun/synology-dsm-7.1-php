#!/usr/bin/env bash
set -Eeuo pipefail
umask 022

ROOT=/build
WORK=/work
VARIANT_ENV=$(python3 "$WORK/scripts/variant_config.py" --shell)
eval "$VARIANT_ENV"
JOBS=${BUILD_JOBS:-4}
TC=$ROOT/toolchain/x86_64-pc-linux-gnu
HOST=x86_64-pc-linux-gnu
SYSROOT=$TC/$HOST/sys-root
DEPS=$ROOT/deps
STAGE=$ROOT/stage$PREFIX
export PATH="$TC/bin:$PATH"
export CC=$HOST-gcc CXX=$HOST-g++ AR=$HOST-ar RANLIB=$HOST-ranlib
export NM=$HOST-nm STRIP=$HOST-strip LD=$HOST-ld
export CFLAGS='-O2 -fPIC -march=bonnell -mtune=bonnell -fstack-protector-strong'
export CXXFLAGS="$CFLAGS"
export CPPFLAGS="-I$DEPS/include"
export LDFLAGS="-L$DEPS/lib -Wl,-z,relro,-z,now"
export PKG_CONFIG_LIBDIR="$DEPS/lib/pkgconfig:$DEPS/share/pkgconfig"
export PKG_CONFIG_PATH="$PKG_CONFIG_LIBDIR"
export PKG_CONFIG=$ROOT/pkg-config-static

mkdir -p "$ROOT"/{src,logs,deps,stage} "$WORK/build/$PHP_SERIES/logs"
INPUT_KEY="$PHP_SERIES:$PHP_VERSION:$(sha256sum "$WORK/sources.lock.json" | cut -d' ' -f1)"
if [[ -f "$ROOT/.input-lock" && "$(cat "$ROOT/.input-lock")" != "$INPUT_KEY" ]]; then
    echo 'The source lock or selected PHP branch changed. Use a fresh dedicated Docker build volume.' >&2
    exit 1
fi
if [[ ! -f "$ROOT/.input-lock" ]]; then printf '%s\n' "$INPUT_KEY" > "$ROOT/.input-lock"; fi
cat > "$PKG_CONFIG" <<'EOF'
#!/bin/sh
exec /usr/bin/pkg-config --static "$@"
EOF
chmod 755 "$PKG_CONFIG"

finish() {
    result=$?
    cp -a "$ROOT/logs/." "$WORK/build/$PHP_SERIES/logs/" || true
    if (( result )); then
        printf 'FAILED status=%s phase=%s\n' "$result" "${PHASE:-setup}" | tee "$WORK/build/$PHP_SERIES/status.txt"
        tail -n 60 "$ROOT/logs/${PHASE:-setup}.log" 2>/dev/null || true
    fi
}
trap finish EXIT

step() {
    PHASE=$1; shift
    printf '%s %s\n' "$(date -u +%FT%TZ)" "$PHASE" | tee "$WORK/build/$PHP_SERIES/status.txt"
    "$@" >"$ROOT/logs/$PHASE.log" 2>&1
    cp "$ROOT/logs/$PHASE.log" "$WORK/build/$PHP_SERIES/logs/$PHASE.log"
}

archive() {
    python3 - "$WORK/sources.lock.json" "$1" <<'PY'
import json,sys
x=json.load(open(sys.argv[1]))
items=x.get('sources', x)
if isinstance(items,dict):
    row=items[sys.argv[2]]
else:
    row=next(r for r in items if r.get('name')==sys.argv[2])
print(row['filename'])
PY
}

unpack() {
    local name=$1 filename archive_name=${2:-$1}
    filename=$(archive "$archive_name")
    mkdir -p "$ROOT/src/$name"
    if [[ ! -e "$ROOT/src/$name/.unpacked" ]]; then
        tar -xf "$WORK/downloads/$filename" -C "$ROOT/src/$name" --strip-components=1
        touch "$ROOT/src/$name/.unpacked"
    fi
}

verify_inputs() {
    python3 "$WORK/scripts/verify-inputs.py"
    if [[ ! -x "$TC/bin/$CC" ]]; then
        mkdir -p "$ROOT/toolchain"
        tar -xf "$WORK/downloads/cedarview-gcc850_glibc226_x86_64-GPL.txz" -C "$ROOT/toolchain"
    fi
    "$CC" --version
    "$CC" -print-sysroot
}

build_zlib() {
    cd "$ROOT/src/zlib"
    if [[ ! -f .security-patch-applied ]]; then
        patch -p1 < "$WORK/downloads/zlib-1.3.2-CVE-2026-85091.patch"
        touch .security-patch-applied
    fi
    CHOST=$HOST ./configure --prefix="$DEPS" --static
    make -j"$JOBS"; make install
}

build_openssl() {
    cd "$ROOT/src/openssl"
    ./Configure linux-x86_64 --prefix="$DEPS" --libdir=lib \
        --openssldir="$PREFIX/etc/ssl" no-shared no-tests
    make -j"$JOBS"; make install_sw
}

build_sqlite3() {
    cd "$ROOT/src/sqlite3"
    ./configure --prefix="$DEPS" --host="$HOST" --disable-shared --enable-static --disable-readline
    make -j"$JOBS"; make install
}

build_oniguruma() {
    cd "$ROOT/src/oniguruma"
    ./configure --prefix="$DEPS" --host="$HOST" --disable-shared --enable-static
    make -j"$JOBS"; make install
}

build_libxml2() {
    cd "$ROOT/src/libxml2"
    ./configure --prefix="$DEPS" --host="$HOST" --disable-shared --enable-static \
        --without-python --without-lzma --without-icu
    make -j"$JOBS"; make install
}

build_curl() {
    cd "$ROOT/src/curl"
    ./configure --prefix="$DEPS" --host="$HOST" --disable-shared --enable-static \
        --with-openssl="$DEPS" --with-zlib="$DEPS" \
        --without-libpsl --without-libidn2 --without-brotli --without-zstd \
        --without-nghttp2 --without-nghttp3 --without-ngtcp2 \
        --without-libssh2 --without-libssh --without-librtmp --without-gssapi \
        --disable-ldap --disable-ldaps \
        --with-ca-bundle="$PREFIX/etc/cacert.pem" --without-ca-path
    make -j"$JOBS"; make install
}

build_libzip() {
    cmake -S "$ROOT/src/libzip" -B "$ROOT/src/libzip/build" \
        -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$DEPS" \
        -DCMAKE_SYSTEM_NAME=Linux -DCMAKE_SYSTEM_PROCESSOR=x86_64 \
        -DCMAKE_C_COMPILER="$TC/bin/$CC" -DCMAKE_CXX_COMPILER="$TC/bin/$CXX" \
        -DCMAKE_FIND_ROOT_PATH="$DEPS;$SYSROOT" \
        -DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=ONLY -DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY \
        -DCMAKE_FIND_ROOT_PATH_MODE_PACKAGE=ONLY -DCMAKE_FIND_ROOT_PATH_MODE_PROGRAM=NEVER \
        -DCMAKE_C_FLAGS="$CFLAGS" -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
        -DBUILD_SHARED_LIBS=OFF -DENABLE_BZIP2=OFF -DENABLE_LZMA=OFF -DENABLE_ZSTD=OFF \
        -DENABLE_COMMONCRYPTO=OFF -DENABLE_GNUTLS=OFF -DENABLE_MBEDTLS=OFF \
        -DENABLE_OPENSSL=ON -DOPENSSL_USE_STATIC_LIBS=TRUE -DOPENSSL_ROOT_DIR="$DEPS" \
        -DBUILD_TOOLS=OFF -DBUILD_REGRESS=OFF -DBUILD_EXAMPLES=OFF -DBUILD_DOC=OFF
    cmake --build "$ROOT/src/libzip/build" -j"$JOBS"
    cmake --install "$ROOT/src/libzip/build"
}

build_php() {
    cd "$ROOT/src/php"
    if [[ ! -d ext/apcu ]]; then cp -a "$ROOT/src/apcu" ext/apcu; fi
    ./buildconf --force
    LIBS='-Wl,--start-group -lcurl -lssl -lcrypto -lz -lxml2 -lsqlite3 -lonig -lzip -ldl -lpthread -lm -Wl,--end-group' \
    EXTENSION_DIR="$PREFIX/usr/local/lib/$PHP_TAG/modules" ./configure \
        --prefix="$PREFIX/usr/local" --bindir="$PREFIX/usr/local/bin" \
        --sbindir="$PREFIX/usr/local/bin" --libdir="$PREFIX/usr/local/lib/$PHP_TAG" \
        --with-config-file-path="$PREFIX/etc" --with-config-file-scan-dir="$PREFIX/etc/conf.d" --disable-rpath \
        --host="$HOST" --program-suffix="$PHP_SUFFIX" --enable-cli --enable-cgi --enable-fpm \
        --with-fpm-user=http --with-fpm-group=http --disable-phpdbg \
        --enable-opcache --disable-opcache-jit --enable-apcu=shared \
        --enable-bcmath=shared --enable-calendar=shared --enable-exif=shared \
        --enable-ftp=shared --enable-mbstring=shared --enable-shmop=shared \
        --enable-sockets=shared --enable-sysvmsg=shared --enable-sysvsem=shared \
        --enable-sysvshm=shared --with-curl=shared --with-openssl=shared \
        --with-zlib=shared --with-zip=shared --enable-mysqlnd \
        --with-mysqli=shared,mysqlnd --with-pdo-mysql=shared,mysqlnd \
        --with-sqlite3=shared --with-pdo-sqlite=shared \
        --enable-dom=shared --enable-simplexml=shared --enable-xml=shared \
        --enable-xmlreader=shared --enable-xmlwriter=shared --with-iconv=shared
    make -j"$JOBS"
    make install INSTALL_ROOT="$ROOT/stage"
    mv "$STAGE/usr/local/bin/php-cgi$PHP_SUFFIX" "$STAGE/usr/local/bin/$PHP_TAG-cgi"
    mv "$STAGE/usr/local/bin/php-fpm$PHP_SUFFIX" "$STAGE/usr/local/bin/$PHP_TAG-fpm"
    mkdir -p "$STAGE/etc/conf.d"
    mkdir -p "$STAGE/etc/ssl"
    cp "$ROOT/src/openssl/apps/openssl.cnf" "$STAGE/etc/ssl/openssl.cnf"
    cp /etc/ssl/certs/ca-certificates.crt "$STAGE/etc/cacert.pem"
    cp php.ini-production "$STAGE/etc/php.ini"
    printf '\n; NAS package paths\nextension_dir=%s/usr/local/lib/%s/modules\ncurl.cainfo=%s/etc/cacert.pem\nopenssl.cafile=%s/etc/cacert.pem\n' "$PREFIX" "$PHP_TAG" "$PREFIX" "$PREFIX" >> "$STAGE/etc/php.ini"
    : > "$STAGE/etc/conf.d/extensions.ini"
    for module in bcmath calendar curl dom exif ftp iconv mbstring mysqli pdo_mysql pdo_sqlite shmop simplexml sockets sqlite3 sysvmsg sysvsem sysvshm xml xmlreader xmlwriter zip zlib openssl; do
        [[ ! -f "$STAGE/usr/local/lib/$PHP_TAG/modules/$module.so" ]] || printf 'extension=%s\n' "$module" >> "$STAGE/etc/conf.d/extensions.ini"
    done
    printf 'zend_extension=opcache.so\nextension=apcu.so\nopcache.jit=0\n' > "$STAGE/etc/conf.d/cache.ini"
    find "$STAGE" -type f -name '*.so' -exec "$STRIP" --strip-unneeded {} +
    "$STRIP" --strip-unneeded "$STAGE/usr/local/bin/$PHP_TAG" "$STAGE/usr/local/bin/$PHP_TAG-cgi" "$STAGE/usr/local/bin/$PHP_TAG-fpm"
    python3 "$WORK/scripts/stage-notices.py" "$STAGE"
    mkdir -p "$WORK/build/$PHP_SERIES/stage"
    cp -a "$STAGE/." "$WORK/build/$PHP_SERIES/stage/"
}

step inputs verify_inputs
for name in zlib openssl sqlite3 oniguruma libxml2 curl libzip apcu; do unpack "$name"; done
unpack php "$PHP_SOURCE"
for name in zlib openssl sqlite3 oniguruma libxml2 curl libzip; do
    if [[ ! -f "$DEPS/.built-$name" ]]; then
        step "$name" "build_$name"
        touch "$DEPS/.built-$name"
    fi
done
step php build_php
printf 'COMPILED PHP %s cedarview DSM7.1\n' "$PHP_VERSION" | tee "$WORK/build/$PHP_SERIES/status.txt"
