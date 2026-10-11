FROM ghcr.io/lavalink-devs/lavalink:4.2.2 AS server

# Java 18 misdetects cgroup v2 on newer kernels and ignores CPU quotas.
# Keep the pinned server artifacts, but use a maintained, container-aware JRE.
FROM eclipse-temurin:21-jre-jammy@sha256:f04fb34e053148344e83317976114ec3f37e4b830ec8bdab5a2fe3cecd7d010b

# Authenticated health probe uses curl; application never exposes its port publicly.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 322 lavalink \
    && useradd --system --uid 322 --gid lavalink --home-dir /opt/Lavalink lavalink \
    && install -d -o lavalink -g lavalink /opt/Lavalink

WORKDIR /opt/Lavalink
COPY --from=server --chown=lavalink:lavalink /opt/Lavalink/ /opt/Lavalink/
USER lavalink
EXPOSE 2333
ENTRYPOINT ["java", "-jar", "Lavalink.jar"]
