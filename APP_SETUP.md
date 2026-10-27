# 猫语 App：语音聊天原型

已实现微信式连续语音聊天。左侧为开发板消息，右侧为 App 消息；每条语音显示时长、时间和发送状态，点击气泡可本地回听。消息及 WAV 文件保存在 App Documents/VoiceChat，退出重开仍保留。同一对话中的“上下文”目前指连续历史消息，尚无语音转文字或猫叫含义识别。

## 操作

1. 保持开发板 J3（nRF USB）连接 Mac。J2 只在烧录调试时需要。
2. 启动桥接：`bash scripts/run-chat.sh`。已有桥接运行时不必重复启动。
3. 启动模拟器 App：`bash scripts/run-ios.sh`，或者在 Xcode 打开 `ios/CatTalk.xcodeproj` 运行 CatTalk。
4. 顶部显示“设备在线”后，按住开发板 **Button 1** 对 INMP441 说话，松开发送。左侧出现语音气泡，点击通过 Mac 扬声器播放。录音过程中开发板喇叭不做本地监听。
5. App 点击“点击说话”，对 Mac 麦克风录音，再点“结束录音并发送”。右侧出现语音气泡，开发板喇叭播放该条语音。点击右侧气泡也可在 App 本地回听。
6. “取消”放弃当前 App 录音。单条最长 60 秒，达到上限自动结束；不足 0.1 秒不保存。正在播放到开发板时需等结束再发送下一条。

首次录音允许麦克风访问。模拟器 Device → Sound 的 Input 选择 MacBook Pro 麦克风，Output 选择 MacBook Pro 扬声器。断线后可点右上角“…”进入设置重连。桥接默认 `ws://127.0.0.1:8765`，仅监听本机。

## 实现

- INMP441 → I2S → nRF5340 → USB CDC → Python 桥接 → WebSocket → App 的左侧消息。
- Mac 麦克风 → iOS 模拟器录音 → 右侧消息 → WebSocket → USB CDC → I2S → MAX98357A。
- 16 kHz / 单声道 / PCM16。设备录音按 Button 1 的按下/松开分段，桥接在松开后发送完整语音。App 录音也在结束后发送完整语音，桥接按 20 ms 帧节奏播放。
- App 每条语音使用 UUID 命名 WAV，历史索引为 history.json；不经过云端。断线或应用退出时标记未完成发送为中断，仍可回听本地录音。
- 开发板连接中断时取消未完成的设备录音。半双工收发避免开发板播放时将扬声器声音再次上传。
- 桥接未做离线消息存储或多端同步；App 断开期间的设备语音不会补发。卸载 App 会移除本地历史，当前无导出和删除界面。
- 目前是 USB + Mac 桥接的模拟器原型；尚未实现 iPhone 真机无线连接。

## 固件与依赖

接线沿用 WIRING.md / MIC_SETUP.md：BCLK P1.15、WS P1.12、功放 DIN P1.13、麦克风 SD P1.14、INMP441 L/R 接地，电源共地。无需改线。

```bash
bash scripts/build.sh app
bash scripts/flash.sh 1050072057 app
```

桥接首次安装依赖：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r bridge/requirements.txt
```

本机已安装依赖。需要 Python、Xcode、本机 Nordic SDK v2.9.2 及 J-Link，路径见 scripts/build.sh 和 scripts/flash.sh。

## 本轮验证（2026-10-27）

- App 和固件编译通过，J-Link 烧录校验通过。
- 用户确认：Button 1 录音松开后左侧出现语音，点击可以听到。
- 用户确认：App 录音发送后右侧出现语音，开发板播放清晰。
- 重启 App 后两条历史语音仍在，已触发历史气泡回放；WAV 文件的 16 kHz / 单声道 / PCM16 及索引时长校验通过。
- 协议及语音分段/播放测试 7 项通过：CRC 重同步、碎包、无按键不录音、松开只生成一条完整语音、短按/断线取消、60 秒上限、播放完成后静音。
- 发送完成表示已按时序写入开发板，不代表软件能测量喇叭的实际声学输出；本次实际声音由用户确认。

测试记录位于 artifacts/chat-tests.log、ios-chat-build.log、chat-firmware-build.log 和 chat-firmware-flash.log。
