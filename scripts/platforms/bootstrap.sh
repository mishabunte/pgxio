#!/bin/sh
# Run only inside a disposable platform container or VM.
set -eu
case "$(uname -s)" in
 Linux)
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends ca-certificates curl gnupg
  . /etc/os-release
  install -d /usr/share/postgresql-common/pgdg
  curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
  printf 'deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt %s-pgdg main\n' "$VERSION_CODENAME" > /etc/apt/sources.list.d/pgdg.list
  apt-get update
  apt-get install -y --no-install-recommends build-essential git libclang-dev libpcre2-dev libssl-dev zlib1g-dev libpq-dev pkg-config bison flex python3 python3-venv postgresql-17 postgresql-client-17 patch xz-utils
  ;;
 FreeBSD)
  export ASSUME_ALWAYS_YES=yes
  pkg install -y ca_root_nss curl git gmake llvm pcre2 openssl pkgconf bison flex python3 postgresql17-server postgresql17-client
  python_tag=$(python3 -c 'import sys; print(str(sys.version_info.major)+str(sys.version_info.minor))')
  pkg install -y "py${python_tag}-pip"
  ;;
 *) echo 'Unsupported build host' >&2; exit 1;;
esac
export CARGO_HOME=/opt/pgxio-toolchain/cargo RUSTUP_HOME=/opt/pgxio-toolchain/rustup
if [ ! -x "$CARGO_HOME/bin/rustup" ]; then
 curl -fsSL https://sh.rustup.rs -o /tmp/pgxio-rustup.sh
 sh /tmp/pgxio-rustup.sh -y --no-modify-path --profile minimal --default-toolchain 1.99.1
fi
"$CARGO_HOME/bin/rustup" toolchain install 1.99.1 --profile minimal
