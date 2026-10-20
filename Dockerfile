# Bot + everything `bash b` needs to build AOSP/YAAP inside the container.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        bc bison build-essential ca-certificates ccache curl flex fontconfig \
        g++-multilib gcc-multilib git git-lfs gnupg gperf imagemagick lib32readline-dev \
        lib32z1-dev libelf-dev libgl1 libncurses-dev libsdl1.2-dev libssl-dev \
        libxml2 libxml2-utils lz4 lzop ncurses-bin openssh-client openssl pngcrush procps python3 \
        python-is-python3 rsync schedtool squashfs-tools tzdata unzip xsltproc \
        zip zlib1g-dev zstd \
    && rm -rf /var/lib/apt/lists/*

# Run as the same uid/gid/home as the host user so file ownership and the
# absolute paths baked into out/ and `b` ($HOME/y/...) stay identical.
ARG UID=1000 GID=1000 USERNAME=builder
RUN (userdel -r ubuntu 2>/dev/null || true) \
    && groupadd -g ${GID} ${USERNAME} \
    && useradd -m -u ${UID} -g ${GID} -s /bin/bash ${USERNAME}
USER ${USERNAME}
# pre-create so mounting ~/.cache/ccache does not leave ~/.cache root-owned (go-build cache lives there)
RUN mkdir -p /home/${USERNAME}/.cache
WORKDIR /home/${USERNAME}/buildbot

CMD ["python3", "bot.py"]
