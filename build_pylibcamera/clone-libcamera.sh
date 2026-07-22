#!/bin/bash
set -e
rm -rf libcamera
git clone --depth 1 --branch "$2" "$1" libcamera
pushd libcamera
git apply ../$3.patch
popd
