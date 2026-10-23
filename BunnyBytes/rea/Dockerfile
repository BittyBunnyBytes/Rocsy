# Rocsy REA: "Reverse Engineer Anything" (github.com/morluto/rea) as an MCP service for Hermes.
# REA itself speaks MCP over stdio; supergateway serves it as Streamable HTTP (one REA process per
# MCP session) on 127.0.0.1:8975; web/server.mjs serves the /REA page on 127.0.0.1:8976. Prerequisites come from versions.json (SHA-256 pinned) or Debian.
# Updates: Models -> Manager -> Core Backend -> REA (hostctl/coreextras.py rewrites versions.json).
FROM node:24-trixie-slim
# apt over plain HTTP: on some networks deb.debian.org (Fastly) answers with spurious
# "400 Bad Request" or drops connections, which apt never retries. No pipelining, and an
# apt-get shim (first on PATH) that reruns failed commands; finished .debs stay cached.
RUN printf 'Acquire::http::Pipeline-Depth "0";\nAcquire::Retries "5";\n' > /etc/apt/apt.conf.d/80-rocsy-network \
    && printf '#!/bin/sh\nn=0\nuntil /usr/bin/apt-get "$@"; do\n  n=$((n + 1)); [ "$n" -ge 4 ] && exit 1\n  echo "apt-get failed, retrying ($n/3)" >&2; sleep $((n * 5))\ndone\n' > /usr/local/sbin/apt-get \
    && chmod 755 /usr/local/sbin/apt-get
ENV NODE_ENV=production DEBIAN_FRONTEND=noninteractive
# APT_STAMP changes on every Manager install/update so Debian tools pick up security updates.
ARG APT_STAMP=0
RUN echo "apt refresh ${APT_STAMP}" && apt-get update && apt-get install --no-install-recommends -y \
      openjdk-21-jdk-headless binutils gdb tshark sigrok-cli binwalk adb procps util-linux \
      chromium fonts-liberation python3 python3-pwntools unzip xz-utils ca-certificates lua5.4 libfontconfig1 xvfb xauth xdotool \
    && rm -rf /var/lib/apt/lists/*
COPY versions.json fetch-tools.mjs /opt/rea-src/
RUN node /opt/rea-src/fetch-tools.mjs /opt/rea-src/versions.json /opt/rea-tools
RUN npm install --global --no-audit --no-fund \
      "rea-agents@$(node -p "require('/opt/rea-src/versions.json')['rea-agents'].version")" \
      "supergateway@$(node -p "require('/opt/rea-src/versions.json').supergateway.version")" \
      "@wakaru/cli@$(node -p "require('/opt/rea-src/versions.json').wakaru.version")" \
      "@electron/asar@$(node -p "require('/opt/rea-src/versions.json').asar.version")" \
    && npm cache clean --force
# ILSpy (C# decompiler for Unity/.NET game code) and the .NET Framework reference assemblies
# BepInEx 5 plugins compile against, restored into a cache the node user can extend.
ENV DOTNET_ROOT=/opt/rea-tools/dotnet DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1 \
    DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 NUGET_PACKAGES=/opt/nuget DOTNET_CLI_HOME=/tmp/dotnet-home
RUN /opt/rea-tools/dotnet/dotnet tool install --tool-path /opt/rea-tools/ilspy ilspycmd \
      --version "$(node -p "require('/opt/rea-src/versions.json').ilspycmd.version")" \
    && mkdir -p /tmp/refs && cd /tmp/refs \
    && printf '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net472</TargetFramework></PropertyGroup><ItemGroup><PackageReference Include="Microsoft.NETFramework.ReferenceAssemblies" Version="%s" PrivateAssets="all" /></ItemGroup></Project>' \
      "$(node -p "require('/opt/rea-src/versions.json')['netfx-refs'].version")" > refs.csproj \
    && /opt/rea-tools/dotnet/dotnet restore \
    && printf '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net6.0</TargetFramework></PropertyGroup></Project>' > net6.csproj \
    && rm refs.csproj && /opt/rea-tools/dotnet/dotnet restore net6.csproj && cd / && rm -rf /tmp/refs /tmp/dotnet-home \
    && mkdir -p /tmp/dotnet-home && chown -R node:node /opt/nuget /tmp/dotnet-home
COPY manifest.sh /opt/rea-src/
RUN sh /opt/rea-src/manifest.sh > /opt/rea-tools/manifest.json && cat /opt/rea-tools/manifest.json
ENV JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64 \
    GHIDRA_INSTALL_DIR=/opt/rea-tools/ghidra \
    REA_ANALYSIS_PROVIDER=ghidra \
    REA_JADX_MCP_JAR=/opt/rea-tools/jadx/jadx-headless-mcp.jar \
    REA_WABT_BIN_DIRECTORY=/opt/rea-tools/wabt/bin \
    REA_APKTOOL_COMMAND=/opt/rea-tools/bin/apktool \
    REA_RIZIN_COMMAND=/opt/rea-tools/bin/rizin \
    REA_TSHARK_COMMAND=/usr/bin/tshark \
    REA_CAPINFOS_COMMAND=/usr/bin/capinfos \
    REA_BINWALK_COMMAND=/usr/bin/binwalk \
    REA_ADB_PATH=/usr/bin/adb \
    REA_BROWSER_EXECUTABLE=/usr/bin/chromium \
    REA_PWNTOOLS_PYTHON=/usr/bin/python3 \
    REA_WAKARU_COMMAND=/usr/local/bin/wakaru \
    PATH=/opt/rea-tools/bin:/opt/rea-tools/dotnet:/opt/rea-tools/ilspy:$PATH \
    REA_PORT=8975
COPY web /opt/rea-web
USER node
WORKDIR /home/node
EXPOSE 8975 8976
# server.mjs serves the /REA page on 8976 and keeps supergateway (Hermes' MCP endpoint, 8975) running.
CMD ["node", "/opt/rea-web/server.mjs"]
