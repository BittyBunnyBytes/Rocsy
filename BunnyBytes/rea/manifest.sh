#!/bin/sh
# Installed versions for the Manager card: pinned downloads + npm packages + Debian tools.
set -e
apt_json=$(dpkg-query -W -f='"${Package}": "${Version}",\n' openjdk-21-jdk-headless binutils gdb tshark sigrok-cli \
  binwalk adb chromium python3-pwntools | sed '$ s/,$//')
node -e '
const pins = require("/opt/rea-tools/pins.json");
const npm = (p) => require(`/usr/local/lib/node_modules/${p}/package.json`).version;
const out = {
  downloads: Object.fromEntries(["ghidra", "jadx-headless-mcp", "wabt", "apktool", "rizin", "dotnet-sdk",
    "bepinex-win-x64", "bepinex-win-x86", "bepinex-linux-x64", "bepinex6-il2cpp-win-x64", "bepinex6-il2cpp-linux-x64",
    "ue4ss", "gdre", "unrpyc"].map((k) => [k, pins[k].version])),
  npm: {"rea-agents": npm("rea-agents"), supergateway: npm("supergateway"), wakaru: npm("@wakaru/cli"),
    ilspycmd: pins.ilspycmd.version, "netfx-refs": pins["netfx-refs"].version, asar: npm("@electron/asar")},
  apt: JSON.parse(process.argv[1]), built: new Date().toISOString(),
};
console.log(JSON.stringify(out));
' "{$apt_json}"
