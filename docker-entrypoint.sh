#!/usr/bin/env sh

USERNAME=root
SSH_DIR="/root/.ssh"

if [[ -n "${SSH_KEY_PATH}" ]]; then
	mkdir -p "${SSH_DIR}"
	cp ${SSH_KEY_PATH} "${SSH_DIR}/id_rsa"
    chmod -f 600 "${SSH_DIR}/id_rsa"
    chown -R $USERNAME:$USERNAME "${SSH_DIR}"
fi

exec python /deploy.py "${@}"