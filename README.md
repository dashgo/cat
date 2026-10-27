# 当前项目：猫语语音聊天 App

当前开发板使用 app-firmware，已实现 Button 1 录音发送、App 聊天气泡回放、App 录音发送到开发板播放及本地历史。使用与启动见 [APP_SETUP.md](APP_SETUP.md)。以下是早期 USB 音箱阶段的历史记录，所述固件状态不代表当前状态。

# CAT USB Speaker

nRF5340 DK（PCA10095）+ MAX98357A + 8Ω 喇叭的 USB 音频播放固件。
基于本机 nRF Connect SDK v2.9.2 / Zephyr 的 uac2_explicit_feedback 示例；保留原文件版权。

## 功能

- USB Audio Class 2，48 kHz / 16-bit / 双声道输入。
- 左右声道混音，再复制到 I2S 两个声道，单只喇叭不丢失另一声道内容。
- 固定 25% 数字幅度（约 -12 dB）；播放器可进一步降低音量。
- 使用原示例异步显式反馈、缓冲管理和欠载恢复。
- 当前仅播放；INMP441 录音及猫叫识别不包含在本固件中。

## 接线

| nRF5340 DK | MAX98357A |
|---|---|
| P1.15 | BCLK |
| P1.12 | LRC / LRCLK |
| P1.13 | DIN |
| GND | GND |

功放由符合模块要求的电源供电并共地，SD/EN 必须启用。
喇叭接 SPK+ 与 SPK-，两端都不能接地。先核对喇叭额定功率。
本固件不驱动麦克风，已接麦克风时不要把其 SD 输出接到功放 DIN/MCU SDOUT。

## 编译与烧录

```bash
bash scripts/build.sh
bash scripts/flash.sh
```

脚本固定使用当前机器的 SDK v2.9.2 和配套 b8efef2ad5 工具链。
默认调试器序列号 1050072057；也可将其他序列号作为 flash.sh 的首个参数。
产物：build/firmware/zephyr/zephyr.hex。
首次烧录前的应用核 1 MiB Flash 备份：artifacts/original-app-flash.bin。
这不是整个设备备份，不包含网络核和 UICR。

## Mac 播放

1. 保留 J2 调试 USB；另外用数据线把 J3（nRF USB）接到 Mac。
2. 系统设置 → 声音 → 输出，选择 CAT USB Speaker。
3. 音频 MIDI 设置中确认 48,000 Hz、2 声道、16 位。
4. 在音乐播放器内降低音量后播放；系统音量滑块可能不可用。
5. 验收连续播放、暂停恢复和重新插拔；实际是否发声需现场确认。

本固件未实现主机音量 Feature Unit；固定数字增益位于 firmware/src/main.c 的 PCM 混音处。
样例 USB VID/PID 仅用于开发，不作为量产 USB 身份。

## 检查

```bash
system_profiler SPAudioDataType
ioreg -p IOUSB -w 0
```

如果 USB 树只有 J-Link 而没有 CAT USB Speaker，检查 J3 数据连接。
若识别成功但无声，检查功放供电、SD/EN、共地、上述 I2S 引脚和 MAX98357A 后缀。

## 来源

https://docs.zephyrproject.org/latest/samples/subsys/usb/uac2_explicit_feedback/README.html

实际工程使用 SDK 内的配套版本，并非最新上游版本。

## 本次验证结果

- 固件编译成功：Flash 70,244 B，RAM 17,868 B。
- 已烧录到序列号 1050072057，J-Link Program & Verify 成功。
- macOS 已枚举 CAT USB Speaker，USB、双声道、48 kHz。
- 已设为 Mac 默认音频输出，并发送 4 秒低音量测试音（先左后右）。
- 实际喇叭是否发声、声音质量和长时间连续播放仍需现场确认。
- 启动串口本次未捕获文本，不能据此判定无错误。

重新选择输出：`swift scripts/select-output.swift`。
验证记录见 artifacts/build.log、artifacts/flash.log、artifacts/audio-devices.txt。

## 无声排查补充

用户反馈外接喇叭无声。USB 设备仍存在并为默认输出。
已加入 USB 流启停、I2S 启动结果和累计音频统计诊断。
通过 SWD 读取本次构建运行变量：rx_bytes=0x00A9A208，rx_peak=0x058F，tx_errors=1；I2S started 标志为真。
这证明接收过非零音频并进入 I2S 发送流程，不能证明物理 I2S 波形或喇叭正常。
串口曾捕获反馈端点 0x88 请求失败，随后采集存在乱码；原因尚未确定，未声称修复。
仍需核对实物接线、功放供电和 SD/EN 状态，并现场确认发声。
只有一根数据线时，烧录后可以从 J2 移到 J3 播放；双线仅便于同时调试。

## 接线照片分析更新

IMG_2870.HEIC 显示功放整排直插开发板 GPIO 排座，电源和信号未按功能匹配。请先断电并按 [WIRING.md](WIRING.md) 使用独立跳线重接。现有固件引脚与该表一致，不能通过软件将普通 GPIO 替代功放电源。改线之前停止播放测试，尚未完成实际发声验收。

## 重新接线后的现场验证

用户按 WIRING.md 重新接线后，Mac 再次枚举 CAT USB Speaker；已选为默认输出并完成 4 秒低音量测试音播放。用户确认“听到了清晰的测试音”。至此 USB → nRF5340 → I2S → MAX98357A → 喇叭的实际发声已获现场确认。

本轮没有重新烧录固件。串口仍捕获反馈端点请求失败，且只有不完整的统计行；不将其描述为已修复。长时间歌曲播放稳定性尚未验证。如有断续、杂音或播放停止，再结合实际症状处理 USB 反馈问题。

## INMP441 实时外放工程

已新增 mic-firmware，接线、按住 Button 1 外放的操作及固件切换见 [MIC_SETUP.md](MIC_SETUP.md)。已上板验证 32 位时隙全双工收发，实际麦克风语音尚待可靠接线。当前开发板已恢复 USB 音箱版本。

## 完成接线后的整体验证

见 [TEST_RESULTS.md](TEST_RESULTS.md)。USB 测试音和麦克风按键外放均已由用户确认清晰；麦克风固件短时运行累计 18436 块、收发错误为零。当前开发板保留麦克风外放固件，实际语音已验收。
