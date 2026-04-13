#!/bin/sh
set -eu

: "${KOPF_NAMESPACE:=default}"
exec kopf run --standalone --verbose --namespace "${KOPF_NAMESPACE}" main.py