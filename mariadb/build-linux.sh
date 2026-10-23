#!/usr/bin/env bash
set -Eeuo pipefail
umask 022
JOBS=${BUILD_JOBS:-4}
ROOT=/build
WORK=/work
TC=$ROOT/toolchain/x86_64-pc-linux-gnu
HOST=x86_64-pc-linux-gnu
SYSROOT=$TC/$HOST/sys-root
DEPS=$ROOT/deps
eval "$(python3 /work/config.py)"
export PATH="$TC/bin:$PATH"
export CC=$HOST-gcc CXX=$HOST-g++ AR=$HOST-ar RANLIB=$HOST-ranlib
export CFLAGS='-O2 -fPIC -march=bonnell -mtune=bonnell -fstack-protector-strong'
export CXXFLAGS="$CFLAGS"
export PKG_CONFIG_LIBDIR="$DEPS/lib/pkgconfig:$SYSROOT/usr/lib/pkgconfig"
mkdir -p "$ROOT/logs" "$WORK/build" "$DEPS"
python3 - <<'PY'
import hashlib
from pathlib import Path
from config import SOURCES
for row in SOURCES.values():
    with (Path('/work/downloads') / row['filename']).open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != row['sha256']:
            raise RuntimeError('Input SHA256 mismatch: ' + row['name'])
PY
mkdir -p "$ROOT/toolchain"
if [[ ! -x "$TC/bin/$CC" ]]; then
    toolchain=$(python3 -c 'from config import SOURCES; print(SOURCES["synology-toolchain"]["filename"])')
    tar -xf "$WORK/downloads/$toolchain" -C "$ROOT/toolchain"
fi
for source_name in openssl mariadb ncurses; do
    source_archive=$(python3 -c 'from config import SOURCES; import sys; print(SOURCES[sys.argv[1]]["filename"])' "$source_name")
    source_dir=$source_name
    if [[ "$source_name" == mariadb ]]; then source_dir=source; fi
    mkdir -p "$ROOT/$source_dir"
    if [[ ! -f "$ROOT/$source_dir/.unpacked" ]]; then
        tar -xf "$WORK/downloads/$source_archive" -C "$ROOT/$source_dir" --strip-components=1
        touch "$ROOT/$source_dir/.unpacked"
    fi
done
PHASE=setup
finish() {
    result=$?
    cp -a "$ROOT/logs/." "$WORK/build/" || true
    if (( result )); then
        printf 'FAILED phase=%s status=%s\n' "$PHASE" "$result" > "$WORK/build/status.txt"
        tail -n 65 "$ROOT/logs/$PHASE.log" 2>/dev/null || true
    fi
}
trap finish EXIT
step() {
    PHASE=$1; shift
    printf '%s %s\n' "$(date -u +%FT%TZ)" "$PHASE" | tee "$WORK/build/status.txt"
    "$@" > "$ROOT/logs/$PHASE.log" 2>&1
    cp "$ROOT/logs/$PHASE.log" "$WORK/build/$PHASE.log"
}
openssl_build() {
    cd "$ROOT/openssl"
    ./Configure linux-x86_64 --prefix="$DEPS" --libdir=lib \
        no-shared no-tests no-docs no-module
    make -j"$JOBS"
    make install_sw
}
ncurses_build() {
    cd "$ROOT/ncurses"
    ./configure --host="$HOST" --prefix="$DEPS" --without-shared --with-normal --without-debug \
        --without-ada --without-cxx --without-cxx-binding --without-manpages --without-tests \
        --without-progs --disable-stripping --enable-pc-files --with-pkg-config-libdir="$DEPS/lib/pkgconfig"
    make -j"$JOBS"
    make install
}
configure() {
    printf '#include <curses.h>\n#include <term.h>\nint main(void) { int e; return setupterm(0, 1, &e); }\n' \
        | "$CC" $CFLAGS -Werror=implicit-function-declaration -I"$DEPS/include" -x c -c -o "$ROOT/curses-header-check.o" -
    cmake -S "$ROOT/source" -B "$ROOT/mariadb-build" -G Ninja \
        -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$PREFIX" -DINSTALL_LAYOUT=STANDALONE \
        -DCMAKE_C_COMPILER="$TC/bin/$CC" -DCMAKE_CXX_COMPILER="$TC/bin/$CXX" \
        -DCMAKE_C_FLAGS="$CFLAGS" -DCMAKE_CXX_FLAGS="$CXXFLAGS" \
        -DCMAKE_EXE_LINKER_FLAGS='-static-libstdc++ -static-libgcc -Wl,-z,relro,-z,now' \
        -DCMAKE_SHARED_LINKER_FLAGS='-static-libstdc++ -static-libgcc -Wl,-z,relro,-z,now' \
        -DCMAKE_MODULE_LINKER_FLAGS='-static-libstdc++ -static-libgcc -Wl,-z,relro,-z,now' \
        -DCMAKE_C_STANDARD_LIBRARIES='-ldl -lpthread' -DCMAKE_CXX_STANDARD_LIBRARIES='-ldl -lpthread' \
        -DCMAKE_FIND_ROOT_PATH="$DEPS;$SYSROOT" \
        -DCMAKE_PREFIX_PATH="$DEPS" \
        -DCMAKE_FIND_ROOT_PATH_MODE_PROGRAM=NEVER -DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY \
        -DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=ONLY -DCMAKE_FIND_ROOT_PATH_MODE_PACKAGE=ONLY \
        -DWITH_SSL="$DEPS" -DOPENSSL_ROOT_DIR="$DEPS" -DOPENSSL_USE_STATIC_LIBS=TRUE \
        -DWITH_PCRE=bundled -DWITH_LIBFMT=bundled -DWITH_ZLIB=bundled -DWITH_READLINE=ON \
        -DCURSES_LIBRARY="$DEPS/lib/libncursesw.a" -DCURSES_INCLUDE_PATH="$DEPS/include/ncursesw" \
        -DHAVE_TERM_H=ON \
        -DWITH_SYSTEMD=no -DWITH_WSREP=OFF -DWITH_UNIT_TESTS=OFF -DWITH_EMBEDDED_SERVER=OFF \
        -DWITH_LIBURING=OFF -DWITH_NUMA=OFF -DWITH_INNODB_BZIP2=OFF -DWITH_INNODB_LZMA=OFF \
        -DWITH_INNODB_LZ4=OFF -DWITH_INNODB_LZO=OFF -DWITH_INNODB_SNAPPY=OFF \
        -DPLUGIN_ROCKSDB=NO -DPLUGIN_MROONGA=NO -DPLUGIN_COLUMNSTORE=NO -DPLUGIN_SPIDER=NO \
        -DPLUGIN_S3=NO -DPLUGIN_CONNECT=NO -DPLUGIN_DUCKDB=NO -DPLUGIN_AUTH_PAM=NO \
        -DPLUGIN_AUTH_PAM_V1=NO -DPLUGIN_AUTH_GSSAPI=NO -DPLUGIN_GSSAPI=NO \
        -DMYSQL_MAINTAINER_MODE=NO -DDEFAULT_CHARSET=utf8mb4 -DDEFAULT_COLLATION=utf8mb4_unicode_ci
}
install_build() {
    DESTDIR="$ROOT/stage" cmake --install "$ROOT/mariadb-build" --strip
}
if [[ ! -f "$DEPS/lib/libssl.a" ]]; then step openssl openssl_build; fi
if [[ ! -f "$DEPS/lib/libncursesw.a" ]]; then step ncurses ncurses_build; fi
for ncurses_header in "$DEPS"/include/ncursesw/*.h; do
    ncurses_name=${ncurses_header##*/}
    if [[ ! -e "$DEPS/include/$ncurses_name" ]]; then
        ln -s "ncursesw/$ncurses_name" "$DEPS/include/$ncurses_name"
    fi
done
if ! grep -q 'cedarview-parser-optimization' "$ROOT/source/sql/CMakeLists.txt"; then
    cat >> "$ROOT/source/sql/CMakeLists.txt" <<'EOF'

# cedarview-parser-optimization: GCC 8 spends excessive time optimizing the generated parsers.
set_property(SOURCE ${CMAKE_CURRENT_BINARY_DIR}/yy_mariadb.cc ${CMAKE_CURRENT_BINARY_DIR}/yy_oracle.cc
             APPEND PROPERTY COMPILE_OPTIONS -O1)
EOF
fi
step configure configure
step compile cmake --build "$ROOT/mariadb-build" --parallel "$JOBS"
step install install_build
printf 'BUILD_COMPLETE\n' | tee "$WORK/build/status.txt"
