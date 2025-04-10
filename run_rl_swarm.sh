#!/bin/bash

# 统一的配置管理
ROOT=$PWD

# 环境变量设置（优先使用环境变量，否则使用默认值）
set_config_value() {
    local var_name=$1
    local default_value=$2
    eval $var_name=${!var_name:-$default_value}
}

# 设置默认值
set_config_value PUB_MULTI_ADDRS ""
set_config_value PEER_MULTI_ADDRS "/ip4/38.101.215.13/tcp/30002/p2p/QmQ2gEXoPJg6iMBSUFWGzAabS2VhnzuS782Y637hGjfsRJ"
set_config_value HOST_MULTI_ADDRS "/ip4/0.0.0.0/tcp/38331"
set_config_value IDENTITY_PATH "$ROOT/swarm.pem"
set_config_value CONNECT_TO_TESTNET False

# 服务器相关设置
HF_HUB_DOWNLOAD_TIMEOUT=120  # 2分钟
GREEN_TEXT="\033[32m"
RESET_TEXT="\033[0m"

echo_green() {
    echo -e "$GREEN_TEXT$1$RESET_TEXT"
}

# 确认是否连接到Testnet
confirm_connection() {
    while true; do
        echo -en $GREEN_TEXT
        read -p ">> Would you like to connect to the Testnet? [Y/n] " yn
        echo -en $RESET_TEXT
        yn=${yn:-Y}  # 默认选择"Y"
        case $yn in
            [Yy]*) CONNECT_TO_TESTNET=True && break ;;
            [Nn]*) CONNECT_TO_TESTNET=False && break ;;
            *) echo ">>> Please answer yes or no." ;;
        esac
    done
}

# 安装yarn的函数
install_yarn() {
    echo "检测yarn是否安装..."
    source ~/.bashrc
    if ! command -v yarn > /dev/null 2>&1; then
        echo "yarn未安装，开始安装..."
        if grep -qi "ubuntu" /etc/os-release 2>/dev/null || uname -r | grep -qi "microsoft"; then
            echo "检测到Ubuntu系统，开始通过apt安装yarn..."
            curl -sS https://dl.yarnpkg.com/debian/pubkey.gpg | sudo apt-key add -
            echo "deb https://dl.yarnpkg.com/debian/ stable main" | sudo tee /etc/apt/sources.list.d/yarn.list
            sudo apt update && sudo apt install -y yarn
        else
            echo "非Ubuntu系统，直接通过脚本安装yarn..."
            curl -o- -L https://yarnpkg.com/install.sh | sh
            echo 'export PATH="$HOME/.yarn/bin:$HOME/.config/yarn/global/node_modules/.bin:$PATH"' >> ~/.bashrc
            source ~/.bashrc
        fi
    fi
}

# 统一安装依赖的函数
pip_install() {
    pip install --disable-pip-version-check -q -r "$1"
}

# 启动modal-login并等待API Key激活
start_modal_login() {
    echo "正在启动modal_login服务器，请登录创建Ethereum服务器钱包..."
    cd modal-login
    install_yarn
    yarn install
    yarn dev > /dev/null 2>&1 &  # 在后台运行并屏蔽输出
    SERVER_PID=$!
    sleep 5
    open http://localhost:3000
    cd ..
    
    echo_green ">> 等待 modal userData.json 创建..."
    while [ ! -f "modal-login/temp-data/userData.json" ]; do
        sleep 5  # 每5秒检查一次
    done
    echo "找到 userData.json，继续..."
    
    ORG_ID=$(awk 'BEGIN { FS = "\"" } !/^[ \t]*[{}]/ { print $(NF - 1); exit }' modal-login/temp-data/userData.json)
    echo "ORG_ID: $ORG_ID"
    
    # 等待API Key激活
    echo "等待API Key激活..."
    while true; do
        STATUS=$(curl -s "http://localhost:3000/api/get-api-key-status?orgId=$ORG_ID")
        if [[ "$STATUS" == "activated" ]]; then
            echo "API Key已激活！继续..."
            break
        else
            echo "API Key未激活，等待中..."
            sleep 5
        fi
    done
}

# 清理函数
cleanup() {
    echo_green ">> 关闭服务器..."
    kill $SERVER_PID
    rm -r modal-login/temp-data/*.json
    exit 0
}

# 捕捉Ctrl+C信号并调用清理函数
trap cleanup INT

# 获取requirements并安装
echo_green ">> 获取并安装依赖..."
pip_install "$ROOT/requirements-hivemind.txt"
pip_install "$ROOT/requirements.txt"

# 根据系统情况安装GPU支持的依赖
if ! command -v nvidia-smi &> /dev/null; then
    CONFIG_PATH="$ROOT/hivemind_exp/configs/mac/grpo-qwen-2.5-0.5b-deepseek-r1.yaml"
elif [ -n "$CPU_ONLY" ]; then
    CONFIG_PATH="$ROOT/hivemind_exp/configs/mac/grpo-qwen-2.5-0.5b-deepseek-r1.yaml"
else
    pip_install "$ROOT/requirements_gpu.txt"
    CONFIG_PATH="$ROOT/hivemind_exp/configs/gpu/grpo-qwen-2.5-0.5b-deepseek-r1.yaml"
fi

# Hugging Face Token设置
echo_green ">> 设置Hugging Face Token..."
if [ -n "${HF_TOKEN}" ]; then
    HUGGINGFACE_ACCESS_TOKEN="${HF_TOKEN}"
else
    read -p ">> 你是否想将训练的模型推送到Hugging Face Hub? [y/N] " yn
    yn=${yn:-N}
    if [[ "$yn" =~ [Yy] ]]; then
        read -p "请输入Hugging Face访问Token: " HUGGINGFACE_ACCESS_TOKEN
    else
        HUGGINGFACE_ACCESS_TOKEN="None"
    fi
fi

echo_green ">> 开始训练模型..."

# 根据ORG_ID是否存在选择不同的训练配置
if [ -n "$ORG_ID" ]; then
    python -m hivemind_exp.gsm8k.train_single_gpu \
        --hf_token "$HUGGINGFACE_ACCESS_TOKEN" \
        --identity_path "$IDENTITY_PATH" \
        --modal_org_id "$ORG_ID" \
        --config "$CONFIG_PATH"
else
    python -m hivemind_exp.gsm8k.train_single_gpu \
        --hf_token "$HUGGINGFACE_ACCESS_TOKEN" \
        --identity_path "$IDENTITY_PATH" \
        --public_maddr "$PUB_MULTI_ADDRS" \
        --initial_peers "$PEER_MULTI_ADDRS" \
        --host_maddr "$HOST_MULTI_ADDRS" \
        --config "$CONFIG_PATH"
fi

# 启动ngrok，保持公网访问
echo_green ">> 启动ngrok..."
ngrok http 38331 &

# 崩溃重启机制
echo_green ">> 启动崩溃重启监控..."
while true; do
    # 启动主要服务
    python -m hivemind_exp.gsm8k.train_single_gpu \
        --hf_token "$HUGGINGFACE_ACCESS_TOKEN" \
        --identity_path "$IDENTITY_PATH" \
        --modal_org_id "$ORG_ID" \
        --config "$CONFIG_PATH"

    # 如果服务崩溃，等待一会儿后重启
    echo "服务崩溃，正在重启..."
    sleep 5
done

