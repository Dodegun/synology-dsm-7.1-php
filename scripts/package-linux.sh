#!/usr/bin/env bash
set -Eeuo pipefail

VARIANT_ENV=$(python3 /work/scripts/variant_config.py --shell)
eval "$VARIANT_ENV"
STAGE=/build/stage$PREFIX
SDK=/build/toolchain/x86_64-pc-linux-gnu/x86_64-pc-linux-gnu/sys-root
LOADER=$SDK/lib/ld-linux-x86-64.so.2
LIBPATH=$SDK/lib:$SDK/usr/lib:$SDK/../lib64
OUTPUT=/work/dist/$PACKAGE-cedarview-$PHP_VERSION-0001.spk

mkdir -p "$(dirname "$PREFIX")" "$STAGE/misc" /work/dist
if [[ -e "$PREFIX" && ! -L "$PREFIX" ]]; then
    echo 'Container validation prefix already exists as a real directory.' >&2
    exit 1
fi
ln -sfn "$STAGE" "$PREFIX"
export PHP_INI_SCAN_DIR="$STAGE/etc/conf.d"
export GCONV_PATH="$SDK/usr/lib/gconv"
python3 /work/scripts/stage-notices.py "$STAGE"
"$LOADER" --library-path "$LIBPATH" "$STAGE/usr/local/bin/$PHP_TAG" \
    -c "$STAGE/etc/php.ini" /work/package/tools/collect-webstation-defaults.php \
    > /build/$PHP_TAG-default_settings.json
mv /build/$PHP_TAG-default_settings.json "$STAGE/misc/default_settings.json"
mkdir -p "/work/build/$PHP_SERIES/stage/misc"
cp "$STAGE/misc/default_settings.json" "/work/build/$PHP_SERIES/stage/misc/default_settings.json"
python3 /work/scripts/package_spk.py "$STAGE" "$OUTPUT" "$@"
