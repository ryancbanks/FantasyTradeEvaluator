#!/bin/sh
set -eu

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
marker_text='fantasy-trade-evaluator install v1'

for destination in "$install_root" "$install_root.previous" "$bin_root" "$desktop_root"; do
  case "$destination" in
    "$home_root"/*) ;;
    *) echo "Refusing unsafe uninstall destination: $destination" >&2; exit 2 ;;
  esac
done
launcher="$bin_root/fantasy-trade-evaluator"
expected_executable="$install_root/FantasyTradeEvaluator"
if [ -L "$launcher" ] && [ "$(readlink -f -- "$launcher" || true)" = "$expected_executable" ]; then
  rm -f -- "$launcher"
elif [ -e "$launcher" ] || [ -L "$launcher" ]; then
  echo "Left an unowned launcher in place: $launcher" >&2
fi
desktop_file="$desktop_root/fantasy-trade-evaluator.desktop"
if [ -f "$desktop_file" ] && [ ! -L "$desktop_file" ] && \
    grep -Fqx 'X-FantasyTradeEvaluator-Owned=true' "$desktop_file"; then
  rm -f -- "$desktop_file"
elif [ -e "$desktop_file" ] || [ -L "$desktop_file" ]; then
  echo "Left an unowned desktop entry in place: $desktop_file" >&2
fi
for application in "$install_root" "$install_root.previous"; do
  marker="$application/.fantasy-trade-evaluator-install"
  if [ -f "$marker" ] && [ ! -L "$marker" ] && [ "$(cat -- "$marker")" = "$marker_text" ]; then
    rm -rf -- "$application"
  elif [ -e "$application" ] || [ -L "$application" ]; then
    echo "Left an unowned application directory in place: $application" >&2
  fi
done
printf '%s\n' 'Fantasy Trade Evaluator was removed. Weekly data was kept in the application data directory.'
