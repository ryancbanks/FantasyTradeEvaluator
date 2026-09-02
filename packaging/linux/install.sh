#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
target_file="$script_dir/release-target.txt"
if [ -L "$target_file" ] || [ ! -f "$target_file" ] || \
    ! grep -Fqx 'fantasy-trade-evaluator release target v1' "$target_file"; then
  echo "Release target metadata is missing or invalid." >&2
  exit 2
fi
target_system=$(sed -n 's/^system=//p' "$target_file")
target_architecture=$(sed -n 's/^architecture=//p' "$target_file")
if [ "$target_system" != linux ]; then
  echo "This installer does not contain a Linux application." >&2
  exit 2
fi
case $(uname -m) in
  x86_64|amd64) host_architecture=x64 ;;
  aarch64|arm64) host_architecture=arm64 ;;
  *) echo "This Linux CPU architecture is not supported." >&2; exit 2 ;;
esac
case "$target_architecture" in
  x64|arm64) ;;
  *) echo "Release target architecture is invalid." >&2; exit 2 ;;
esac
if [ "$host_architecture" != "$target_architecture" ]; then
  echo "This package is for $target_architecture, not $host_architecture." >&2
  exit 2
fi
case ${HOME:-} in
  /*) ;;
  *) echo "HOME must be an absolute path" >&2; exit 2 ;;
esac

data_root=${XDG_DATA_HOME:-"$HOME/.local/share"}
bin_root=${XDG_BIN_HOME:-"$HOME/.local/bin"}
desktop_root=${XDG_DATA_HOME:-"$HOME/.local/share"}/applications
home_root=$(readlink -f -- "$HOME")
data_root=$(readlink -m -- "$data_root")
bin_root=$(readlink -m -- "$bin_root")
desktop_root=$(readlink -m -- "$desktop_root")
install_root="$data_root/fantasy-trade-evaluator"
backup="$install_root.previous"
launcher="$bin_root/fantasy-trade-evaluator"
desktop_file="$desktop_root/fantasy-trade-evaluator.desktop"

for destination in "$install_root" "$backup" "$bin_root" "$desktop_root"; do
  case "$destination" in
    "$home_root"/*) ;;
    *) echo "Refusing unsafe install destination: $destination" >&2; exit 2 ;;
  esac
done

case "$launcher$desktop_file" in
  *'
'*|*'	'*|*''*) echo "XDG install paths cannot contain control characters" >&2; exit 2 ;;
esac
if [ -e "$launcher" ] && [ ! -L "$launcher" ]; then
  echo "Refusing to replace non-link launcher: $launcher" >&2
  exit 2
fi
if [ -d "$desktop_file" ] && [ ! -L "$desktop_file" ]; then
  echo "Refusing to replace desktop-entry directory: $desktop_file" >&2
  exit 2
fi

mkdir -p -- "$data_root" "$bin_root" "$desktop_root"
stage=""
desktop_stage=""
launcher_backup=""
desktop_backup=""
launcher_touched=0
desktop_touched=0
promoted=0
had_previous=0
complete=0
rollback() {
  status=$?
  trap - EXIT HUP INT TERM
  set +e
  if [ "$complete" -ne 1 ]; then
    if [ "$launcher_touched" -eq 1 ]; then rm -f -- "$launcher"; fi
    if [ -n "$launcher_backup" ] && { [ -e "$launcher_backup" ] || [ -L "$launcher_backup" ]; }; then
      mv -T -- "$launcher_backup" "$launcher"
    fi
    if [ "$desktop_touched" -eq 1 ]; then rm -f -- "$desktop_file"; fi
    if [ -n "$desktop_backup" ] && { [ -e "$desktop_backup" ] || [ -L "$desktop_backup" ]; }; then
      mv -T -- "$desktop_backup" "$desktop_file"
    fi
    if [ "$promoted" -eq 1 ]; then rm -rf -- "$install_root"; fi
    if [ "$had_previous" -eq 1 ] && { [ -e "$backup" ] || [ -L "$backup" ]; }; then
      mv -- "$backup" "$install_root"
    fi
  fi
  if [ -n "$stage" ]; then rm -rf -- "$stage"; fi
  if [ -n "$desktop_stage" ]; then rm -f -- "$desktop_stage"; fi
  if [ -n "$launcher_backup" ]; then rm -f -- "$launcher_backup"; fi
  if [ -n "$desktop_backup" ]; then rm -f -- "$desktop_backup"; fi
  exit "$status"
}
trap rollback EXIT
trap 'exit 1' HUP INT TERM

stage=$(mktemp -d -- "$data_root/.fantasy-trade-evaluator-new.XXXXXX")
if [ -L "$stage" ] || [ ! -d "$stage" ]; then
  echo "Refusing unsafe staging directory: $stage" >&2
  exit 2
fi
stage=$(readlink -f -- "$stage")
case "$stage" in
  "$data_root"/.fantasy-trade-evaluator-new.*) ;;
  *) echo "Refusing unsafe staging directory: $stage" >&2; exit 2 ;;
esac

desktop_stage=$(mktemp -- "$desktop_root/.fantasy-trade-evaluator.desktop.XXXXXX")
if [ -L "$desktop_stage" ] || [ ! -f "$desktop_stage" ]; then
  echo "Refusing unsafe desktop-entry staging file: $desktop_stage" >&2
  exit 2
fi
desktop_stage=$(readlink -f -- "$desktop_stage")
case "$desktop_stage" in
  "$desktop_root"/.fantasy-trade-evaluator.desktop.*) ;;
  *) echo "Refusing unsafe desktop-entry staging file: $desktop_stage" >&2; exit 2 ;;
esac

cp -R -- "$script_dir/app/." "$stage/"
cp -- "$script_dir/fantasy-trade-evaluator.svg" "$stage/fantasy-trade-evaluator.svg"
printf '%s\n' 'fantasy-trade-evaluator install v1' > "$stage/.fantasy-trade-evaluator-install"
chmod 755 "$stage/FantasyTradeEvaluator"
if ! "$stage/FantasyTradeEvaluator" --self-check >/dev/null; then
  echo "The packaged interface and browser extension failed their self-check; the previous install was kept." >&2
  exit 2
fi

# Desktop Entry parsing applies string escaping before Exec argument unquoting.
exec_value=$(printf '%s' "$launcher" | sed \
  -e 's/\\/\\\\\\\\/g' -e 's/"/\\"/g' -e 's/`/\\`/g' \
  -e 's/\$/\\\\$/g' -e 's/%/%%/g')
icon_value=$(printf '%s' "$install_root/fantasy-trade-evaluator.svg" | sed \
  -e 's/\\/\\\\/g' -e 's/ /\\s/g')
{
  printf '%s\n' '[Desktop Entry]' 'Type=Application' 'Name=Fantasy Trade Evaluator'
  printf 'Exec="%s"\n' "$exec_value"
  printf 'Icon=%s\n' "$icon_value"
  printf '%s\n' 'Terminal=false' 'Categories=Utility;Sports;' \
    'X-FantasyTradeEvaluator-Owned=true'
} > "$desktop_stage"
chmod 644 "$desktop_stage"

if [ -L "$launcher" ]; then
  launcher_backup=$(mktemp -- "$bin_root/.fantasy-trade-evaluator-launcher.XXXXXX")
  rm -f -- "$launcher_backup"
  mv -T -- "$launcher" "$launcher_backup"
fi
if [ -e "$desktop_file" ] || [ -L "$desktop_file" ]; then
  desktop_backup=$(mktemp -- "$desktop_root/.fantasy-trade-evaluator-backup.XXXXXX")
  rm -f -- "$desktop_backup"
  mv -T -- "$desktop_file" "$desktop_backup"
fi
if [ -e "$backup" ] || [ -L "$backup" ]; then rm -rf -- "$backup"; fi
if [ -e "$install_root" ] || [ -L "$install_root" ]; then
  mv -- "$install_root" "$backup"
  had_previous=1
fi
mv -- "$stage" "$install_root"
stage=""
promoted=1
desktop_touched=1
mv -T -- "$desktop_stage" "$desktop_file"
desktop_stage=""
launcher_touched=1
ln -sfnT -- "$install_root/FantasyTradeEvaluator" "$launcher"
if [ -n "$launcher_backup" ]; then rm -f -- "$launcher_backup"; launcher_backup=""; fi
if [ -n "$desktop_backup" ]; then rm -f -- "$desktop_backup"; desktop_backup=""; fi
complete=1
trap - EXIT HUP INT TERM
printf 'Installed Fantasy Trade Evaluator. Run: %s\n' "$launcher"
