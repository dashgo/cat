import SwiftUI

@main
struct CatTalkApp: App {
    @StateObject private var link = AudioLink()
    @Environment(\.scenePhase) private var scenePhase
    var body: some Scene {
        WindowGroup {
            ContentView(link: link).preferredColorScheme(.light)
                .onChange(of: scenePhase) { _, phase in
                    if phase != .active && link.talking { link.cancelRecording() }
                }
        }
    }
}

private let green = Color(red: 0.60, green: 0.91, blue: 0.40)
private let ink = Color(red: 0.12, green: 0.16, blue: 0.14)

struct ContentView: View {
    @ObservedObject var link: AudioLink
    @State private var showSettings = false
    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                Image(systemName: "cat.fill").font(.system(size: 23)).foregroundStyle(.white)
                    .frame(width: 44, height: 44).background(ink, in: RoundedRectangle(cornerRadius: 14))
                VStack(alignment: .leading, spacing: 4) {
                    Text("猫语伙伴").font(.system(size: 19, weight: .semibold))
                    HStack(spacing: 5) {
                        Circle().fill(link.hardware ? Color.green : .orange).frame(width: 6, height: 6)
                        Text(link.deviceRecording ? "对方正在录音…" : link.deviceBusy ? "设备正在播放…" : link.hardware ? "设备在线" : "设备未连接")
                            .font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                }
                Spacer()
                Button { showSettings = true } label: {
                    Image(systemName: "ellipsis").font(.system(size: 22, weight: .semibold)).frame(width: 40, height: 44)
                }.accessibilityLabel("连接设置")
            }.padding(.horizontal, 20).padding(.vertical, 12).background(.white)
            Divider()
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 20) {
                        Text("和它的每一声，都留在这里")
                            .font(.system(size: 11)).foregroundStyle(.secondary).padding(.top, 22)
                        Text("开发板按住 Button 1 录音，松开发送\n轻点语音气泡，可随时回听")
                            .font(.system(size: 12)).foregroundStyle(.secondary).multilineTextAlignment(.center)
                            .lineSpacing(5).padding(14).frame(maxWidth: .infinity)
                            .background(.white.opacity(0.7), in: RoundedRectangle(cornerRadius: 12))
                        ForEach(link.messages) { voice in
                            voiceRow(voice).id(voice.id)
                        }
                        if link.deviceRecording {
                            Label("设备正在录音，松开 Button 1 发送", systemImage: "waveform")
                                .font(.system(size: 12)).foregroundStyle(.secondary).padding(12)
                        }
                        Color.clear.frame(height: 1).id("bottom")
                    }.padding(.horizontal, 16).padding(.bottom, 15)
                }.onChange(of: link.messages.count) { _, _ in
                    withAnimation { proxy.scrollTo("bottom", anchor: .bottom) }
                }.onChange(of: link.deviceRecording) { _, _ in
                    withAnimation { proxy.scrollTo("bottom", anchor: .bottom) }
                }.onAppear { proxy.scrollTo("bottom", anchor: .bottom) }
            }
            if !link.errorText.isEmpty {
                HStack {
                    Text(link.errorText).font(.system(size: 12)).foregroundStyle(.red)
                    Spacer()
                    Button { link.errorText = "" } label: { Image(systemName: "xmark") }.accessibilityLabel("关闭提示")
                }.padding(12).background(.white)
            }
            VStack(spacing: 10) {
                if link.talking {
                    HStack(spacing: 12) {
                        Circle().fill(.red).frame(width: 7, height: 7)
                        Text("正在录音  \(link.recordingSeconds)s / 60s").font(.system(size: 13, weight: .medium))
                        Spacer()
                        Button("取消") { link.cancelRecording() }.font(.system(size: 13))
                    }
                }
                Button {
                    if link.connected { link.toggleTalk() } else { link.connect() }
                } label: {
                    HStack(spacing: 9) {
                        Image(systemName: link.talking ? "paperplane.fill" : "mic.fill")
                        Text(link.talking ? "结束录音并发送" : !link.connected ? "连接设备" : "点击说话")
                            .font(.system(size: 16, weight: .semibold))
                    }.frame(maxWidth: .infinity).frame(height: 50)
                        .background(link.talking ? green : Color.white, in: RoundedRectangle(cornerRadius: 12))
                        .overlay(RoundedRectangle(cornerRadius: 12).stroke(.black.opacity(0.07)))
                }.buttonStyle(.plain)
                    .disabled(link.connected && (!link.hardware || link.deviceBusy || link.deviceRecording) && !link.talking)
                Text(link.talking ? "发送后将在开发板喇叭播放" : "语音保存在本机 · 最长 60 秒")
                    .font(.system(size: 10)).foregroundStyle(.secondary)
            }.padding(16).background(Color(red: 0.96, green: 0.96, blue: 0.96))
        }.background(Color(red: 0.93, green: 0.94, blue: 0.93)).foregroundStyle(ink)
            .sheet(isPresented: $showSettings) { settings }
            .task { link.connect() }
    }

    private func voiceRow(_ voice: VoiceMessage) -> some View {
        let mine = voice.source == "app"
        let playing = link.playingID == voice.id
        return VStack(spacing: 10) {
            Text(Date(timeIntervalSince1970: voice.timestamp), format: .dateTime.month().day().hour().minute())
                .font(.system(size: 10)).foregroundStyle(.secondary)
            HStack(alignment: .top, spacing: 8) {
                if mine { Spacer(minLength: 45) } else { avatar(mine: false) }
                VStack(alignment: mine ? .trailing : .leading, spacing: 6) {
                    Text(mine ? "我" : "猫语设备").font(.system(size: 10)).foregroundStyle(.secondary)
                    Button { link.playMessage(voice) } label: {
                        HStack(spacing: 14) {
                            Image(systemName: playing ? "stop.fill" : "speaker.wave.2.fill")
                                .font(.system(size: 19))
                            HStack(spacing: 3) {
                                ForEach(0..<9) { i in
                                    Capsule().frame(width: 3, height: CGFloat([8, 15, 21, 12, 25, 16, 10, 19, 8][i]))
                                }
                            }.opacity(0.55)
                            Spacer(minLength: 0)
                            Text("\(max(1, Int(voice.duration.rounded())))″").font(.system(size: 16, weight: .medium))
                        }.padding(.horizontal, 16).frame(width: 170 + min(60, voice.duration * 2), height: 54)
                            .background(mine ? green : .white, in: RoundedRectangle(cornerRadius: 12))
                    }.buttonStyle(.plain)
                        .accessibilityLabel("播放\(mine ? "我" : "设备")的语音，\(Int(voice.duration))秒")
                    Text(playing ? "正在播放 · 再点停止" : deliveryLabel(voice))
                        .font(.system(size: 10)).foregroundStyle(voice.state == "failed" ? .red : .secondary)
                }
                if mine { avatar(mine: true) } else { Spacer(minLength: 45) }
            }
        }
    }
    private func avatar(mine: Bool) -> some View {
        Image(systemName: mine ? "person.fill" : "cat.fill").font(.system(size: 19))
            .foregroundStyle(mine ? ink : .white).frame(width: 36, height: 36)
            .background(mine ? Color.white : ink, in: RoundedRectangle(cornerRadius: 10))
    }
    private func deliveryLabel(_ voice: VoiceMessage) -> String {
        switch voice.state {
        case "sending": return "正在发送…"
        case "playing": return "开发板正在播放…"
        case "played": return "已发送 · 开发板播放完成"
        case "failed": return "发送失败 · 可本地回听"
        case "interrupted": return "发送中断 · 可本地回听"
        default: return "已收到 · 点击播放"
        }
    }
    private var settings: some View {
        NavigationStack {
            Form {
                Section("设备连接") {
                    TextField("桥接地址", text: $link.endpoint).textInputAutocapitalization(.never).autocorrectionDisabled()
                    Text(link.message).font(.footnote)
                    Button(link.connected ? "断开连接" : "重新连接") {
                        if link.connected { link.disconnect() } else { link.connect() }
                    }
                }
                Section("使用方式") {
                    Text("设备：按住 Button 1 说话，松开后发送到聊天窗口。\nApp：点击说话录音，再点击发送并在开发板播放。\n点击任意语音气泡，在 App 中回听。")
                    Text("收发错误：\(link.boardErrors)")
                }
                Section {
                    Text("当前通过 Mac 的 USB 桥接测试，语音及聊天记录保存在 App 本地。尚未接入猫叫含义识别。")
                        .font(.footnote).foregroundStyle(.secondary)
                }
            }.navigationTitle("聊天设置").toolbar { Button("完成") { showSettings = false } }
        }
    }
}
