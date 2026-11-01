#!/usr/bin/env bash

# Build main docker image
docker build --build-arg HOST_UID=$(id -u) -t ctfenv .

# The agent and challenge servers share this network. It is internal, so containers on it
# can reach each other but not the internet (the agent must not look up solutions online).
networkname="ctfnet"
if docker network inspect $networkname > /dev/null 2>&1; then
    if [ "$(docker network inspect -f '{{.Internal}}' $networkname)" != "true" ]; then
        echo "Network ${networkname} exists but is not internal; recreating it"
        docker network rm $networkname
        docker network create --internal $networkname
    else
        echo "Network ${networkname} already exists and is internal, skip!"
    fi
else
    docker network create --internal $networkname
fi

# Download and unpack Ghidra
if [ ! -d ghidra_11.0.1_PUBLIC ]; then
    wget https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_11.0.1_build/ghidra_11.0.1_PUBLIC_20240130.zip
    unzip ghidra_11.0.1_PUBLIC_20240130.zip
    rm ghidra_11.0.1_PUBLIC_20240130.zip
fi
