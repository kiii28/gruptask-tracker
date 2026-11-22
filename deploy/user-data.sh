#!/bin/bash
# Tempel ke EC2 > Advanced details > User data saat launch instance.
# Instance baru akan otomatis clone repo dan memasang aplikasi.
# Ganti REPO_URL dengan repo Git Anda (public, atau pakai AMI jika repo private).
set -euxo pipefail
REPO_URL="https://github.com/USERNAME/NAMA-REPO.git"
# WAJIB sama di semua instance di belakang Load Balancer (agar token login valid di semua instance)
export SECRET_KEY="GANTI-DENGAN-STRING-ACAK-PANJANG"
export APP_URL="http://DNS-ALB-ATAU-IP"

apt-get update -y
apt-get install -y git
rm -rf /opt/grouptask-src
git clone --depth 1 "$REPO_URL" /opt/grouptask-src

SRC=/opt/grouptask-src
[ -d "$SRC/grouptask-tracker" ] && SRC="$SRC/grouptask-tracker"
bash "$SRC/deploy/setup.sh"
