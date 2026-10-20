#!/bin/bash
# Simulates soong output so you can test the bot without a real build.
# Args = variant names; each one runs a full fake lunch + build, like ~/y/b does.
mkdir -p fake_out
[ $# -eq 0 ] && set -- TEST
for v in "$@"; do
  printf '============================================\nYAAP_VERSION=17-%s-fake-%s\nTARGET_PRODUCT=yaap_fake\n============================================\n' "$v" "$(date +%Y%m%d)"
  sleep 2
  echo "[100% 1/1] bootstrap blueprint"; sleep 2
  echo "[100% 2/2] analyzing Android.bp files and generating ninja file at out/soong/build.ninja"; sleep 3
  for i in $(seq 1 42); do echo "[$((i*100/42))% $i/42] including device/fake/mk_$i.mk"; sleep 0.05; done
  total=300
  for i in $(seq 1 $total); do
    echo "[ $((i*100/total))% $i/$total $(( (total-i)/10 ))s remaining] //frameworks/base:services javac out/soong/.intermediates/module_$i/classes.jar"
    sleep 0.1
  done
  echo "#### build completed successfully (00:40 (mm:ss)) ####"
  head -c 3M /dev/urandom > "fake_out/YAAP-17-$v-fake-$(date +%Y%m%d).zip"
done
