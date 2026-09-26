#!/usr/bin/env bash
# Prints the path of the simple-metrics binary to deploy to the Pi, first
# downloading and verifying it if it isn't already cached. ./wga runs this and
# hands the path to tofu (as TF_VAR_simple_metrics_binary); progress and
# errors go to stderr, so stdout is just the path.
#
# With --cli it prints the path of the release's smq command-line client
# instead (from the same verified archive), or nothing if that release has no
# smq (versions before 0.3.0), so an older pin still deploys the collector.
#
# The release and its SHA-256 are pinned in simple-metrics.pin. The archive
# is checked against that pinned checksum, never against one published next
# to it, so a release altered after the fact is refused. It is downloaded
# from GitHub on this machine, not the Pi, so the Pi needs no access to it.
#
# Settings (all optional):
#   SIMPLE_METRICS_BIN          use this binary instead of the pinned release,
#                               for trying a local build. Not verified. Its
#                               smq is the one beside it, if there is one.
#   SIMPLE_METRICS_PIN_FILE     the pin file (default: simple-metrics.pin)
#   SIMPLE_METRICS_CACHE_DIR    where downloads are kept (default:
#                               ~/.cache/wireguard-ap/simple-metrics)
#   SIMPLE_METRICS_RELEASE_URL  where releases are downloaded from (default:
#                               the project's GitHub releases; for testing)
set -euo pipefail

WANT=binary
case "${1:-}" in
  "") ;;
  --cli) WANT=cli ;;
  *) echo "usage: fetch_simple_metrics.sh [--cli]" >&2; exit 2 ;;
esac

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIN_FILE="${SIMPLE_METRICS_PIN_FILE:-$REPO_DIR/simple-metrics.pin}"
CACHE_DIR="${SIMPLE_METRICS_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/wireguard-ap/simple-metrics}"
RELEASE_URL="${SIMPLE_METRICS_RELEASE_URL:-https://github.com/georgenicoll/simple-metrics/releases/download}"
# The Pi's architecture, as a static (musl) binary: runs whatever its glibc.
TARGET="aarch64-unknown-linux-musl"

note() { echo "fetch_simple_metrics: $*" >&2; }
fail() { note "$*"; exit 1; }

# A locally built binary, for development. Deliberately loud: it bypasses
# the pin, so nothing here says what it is.
if [[ -n ${SIMPLE_METRICS_BIN:-} ]]; then
  [[ -f $SIMPLE_METRICS_BIN && -x $SIMPLE_METRICS_BIN ]] \
    || fail "SIMPLE_METRICS_BIN=$SIMPLE_METRICS_BIN is not an executable file"
  note "WARNING: using $SIMPLE_METRICS_BIN, not the pinned release"
  LOCAL_DIR="$(cd "$(dirname "$SIMPLE_METRICS_BIN")" && pwd)"
  if [[ $WANT == cli ]]; then
    [[ -x $LOCAL_DIR/smq ]] && echo "$LOCAL_DIR/smq"
  else
    echo "$LOCAL_DIR/$(basename "$SIMPLE_METRICS_BIN")"
  fi
  exit 0
fi

[[ -f $PIN_FILE ]] || fail "no pin file at $PIN_FILE"
pin() { sed -n "s/^$1=//p" "$PIN_FILE" | tail -n 1; }
VERSION="$(pin SIMPLE_METRICS_VERSION)"
SHA256="$(pin SIMPLE_METRICS_SHA256)"
# Strict formats: both end up in paths and URLs.
[[ $VERSION =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] \
  || fail "SIMPLE_METRICS_VERSION in $PIN_FILE must look like v1.2.3 (got '$VERSION')"
[[ $SHA256 =~ ^[0-9a-f]{64}$ ]] \
  || fail "SIMPLE_METRICS_SHA256 in $PIN_FILE must be 64 lowercase hex digits"

ASSET="simple-metrics-${VERSION}-${TARGET}.tar.gz"
DIR="$CACHE_DIR/$VERSION"
ARCHIVE="$DIR/$ASSET"
mkdir -p -m 700 "$CACHE_DIR"
mkdir -p -m 700 "$DIR"

sha_of() { sha256sum "$1" | cut -d' ' -f1; }
matches_pin() { [[ -f $1 && "$(sha_of "$1")" == "$SHA256" ]]; }

# Checked on every run, not just after a download: a cached file that no
# longer matches (damaged, or replaced) is fetched again rather than trusted.
if ! matches_pin "$ARCHIVE"; then
  note "downloading $ASSET"
  PART="$(mktemp "$DIR/.download.XXXXXX")"
  trap 'rm -f "$PART"' EXIT
  curl -fsSL --retry 3 --connect-timeout 15 -o "$PART" "$RELEASE_URL/$VERSION/$ASSET" \
    || fail "could not download $RELEASE_URL/$VERSION/$ASSET"
  if ! matches_pin "$PART"; then
    fail "the downloaded $ASSET does not match the pinned checksum (expected $SHA256, got $(sha_of "$PART")) - it has NOT been used"
  fi
  mv -f "$PART" "$ARCHIVE"
fi

BINARY="$DIR/simple-metrics"
PREFIX="simple-metrics-${VERSION}-${TARGET}"
tar -xzf "$ARCHIVE" -C "$DIR" --strip-components=1 "$PREFIX/simple-metrics" \
  || fail "could not extract simple-metrics from $ARCHIVE"
chmod 755 "$BINARY"
# smq came in 0.3.0. Remove a stale one first so a release without it can't
# leave an older release's client in the cache to be deployed.
CLI="$DIR/smq"
rm -f "$CLI"
if tar -tzf "$ARCHIVE" "$PREFIX/smq" &>/dev/null; then
  tar -xzf "$ARCHIVE" -C "$DIR" --strip-components=1 "$PREFIX/smq" \
    || fail "could not extract smq from $ARCHIVE"
  chmod 755 "$CLI"
fi
if [[ $WANT == cli ]]; then
  [[ -f $CLI ]] && echo "$CLI"
else
  echo "$BINARY"
fi
exit 0
