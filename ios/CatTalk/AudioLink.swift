import Foundation
import AVFoundation
import SwiftUI

struct VoiceMessage: Identifiable, Codable {
    var id: String
    var source: String
    var timestamp: Double
    var duration: Double
    var state: String
}

@MainActor
final class AudioLink: NSObject, ObservableObject, AVAudioPlayerDelegate {
    @Published var connected = false
    @Published var hardware = false
    @Published var talking = false
    @Published var deviceRecording = false
    @Published var deviceBusy = false
    @Published var message = "正在连接设备…"
    @Published var errorText = ""
    @Published var inputLevel: Double = 0
    @Published var recordingSeconds = 0
    @Published var messages: [VoiceMessage] = []
    @Published var playingID: String?
    @Published var boardErrors = 0
    @Published var endpoint = "ws://127.0.0.1:8765"

    private var socket: URLSessionWebSocketTask?
    private var receiveTask: Task<Void, Never>?
    private let captureEngine = AVAudioEngine()
    private let streamFormat = AVAudioFormat(standardFormatWithSampleRate: 16000, channels: 1)!
    private var player: AVAudioPlayer?
    private var inputTap = false
    private var recordingPCM = Data()
    private var recordingTimer: Task<Void, Never>?
    private let directory = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("VoiceChat", isDirectory: true)

    override init() {
        super.init()
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            if let data = try? Data(contentsOf: directory.appendingPathComponent("history.json")) {
                messages = try JSONDecoder().decode([VoiceMessage].self, from: data)
                for i in messages.indices where ["sending", "playing"].contains(messages[i].state) {
                    messages[i].state = "interrupted"
                }
            }
        } catch { errorText = "读取聊天记录失败：\(error.localizedDescription)" }
    }

    func connect() {
        guard socket == nil else { return }
        guard let url = URL(string: endpoint), ["ws", "wss"].contains(url.scheme ?? "") else {
            errorText = "请输入有效的 WebSocket 地址"; return
        }
        errorText = ""; message = "正在连接…"
        let ws = URLSession.shared.webSocketTask(with: url)
        socket = ws; ws.resume()
        receiveTask = Task { [weak self] in
            while !Task.isCancelled {
                do {
                    let response = try await ws.receive()
                    guard let self else { return }
                    if case .string(let text) = response { self.receive(text) }
                } catch {
                    guard !Task.isCancelled, let self else { return }
                    self.disconnect()
                    self.errorText = "连接中断，请在连接设置中重连"
                    return
                }
            }
        }
    }

    func disconnect() {
        if talking { finishRecording(send: false) }
        receiveTask?.cancel(); receiveTask = nil
        socket?.cancel(with: .goingAway, reason: nil); socket = nil
        connected = false; hardware = false; deviceRecording = false; deviceBusy = false
        for i in messages.indices where ["sending", "playing"].contains(messages[i].state) {
            messages[i].state = "interrupted"
        }
        saveHistory()
        message = "设备未连接，历史语音仍可播放"
    }

    private func send(_ object: [String: Any]) async throws {
        guard let socket else { throw URLError(.notConnectedToInternet) }
        let data = try JSONSerialization.data(withJSONObject: object)
        try await socket.send(.string(String(decoding: data, as: UTF8.self)))
    }

    func toggleTalk() {
        if talking { finishRecording(); return }
        guard hardware && !deviceRecording && !deviceBusy else { return }
        AVAudioApplication.requestRecordPermission { [weak self] granted in
            Task { @MainActor in
                guard let self else { return }
                if granted { self.startRecording() }
                else { self.errorText = "请允许麦克风访问" }
            }
        }
    }

    private func startRecording() {
        guard hardware && !deviceRecording && !deviceBusy && !talking else { return }
        do {
            stopPlayback()
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playAndRecord, mode: .default, options: [.defaultToSpeaker])
            try session.setPreferredSampleRate(48000)
            try session.setActive(true)
            let input = captureEngine.inputNode
            let format = input.outputFormat(forBus: 0)
            guard format.sampleRate > 0, format.channelCount > 0,
                  let converter = AVAudioConverter(from: format, to: streamFormat) else {
                throw NSError(domain: "CatTalk", code: 1,
                              userInfo: [NSLocalizedDescriptionKey: "请检查模拟器的麦克风输入"])
            }
            recordingPCM.removeAll(keepingCapacity: true)
            recordingSeconds = 0; talking = true
            let target = streamFormat
            input.installTap(onBus: 0, bufferSize: 960, format: format) { [weak self] buffer, _ in
                let capacity = AVAudioFrameCount(Double(buffer.frameLength) * 16000 / format.sampleRate + 32)
                guard let converted = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return }
                var supplied = false
                var error: NSError?
                converter.convert(to: converted, error: &error) { _, status in
                    if supplied { status.pointee = .noDataNow; return nil }
                    supplied = true; status.pointee = .haveData; return buffer
                }
                guard error == nil, let samples = converted.floatChannelData?[0] else { return }
                var pcm = Data(); var peak: Float = 0
                for i in 0..<Int(converted.frameLength) {
                    let value = max(-1, min(1, samples[i])); peak = max(peak, abs(value))
                    var sample = Int16(max(-32768, min(32767, Int(value * 32767)))).littleEndian
                    withUnsafeBytes(of: &sample) { pcm.append(contentsOf: $0) }
                }
                let result = pcm; let level = Double(peak)
                Task { @MainActor in
                    guard let self, self.talking else { return }
                    self.inputLevel = level
                    self.recordingPCM.append(result.prefix(max(0, 1920000 - self.recordingPCM.count)))
                }
            }
            inputTap = true
            try captureEngine.start()
            errorText = ""; message = "正在录音，再点一次发送"
            recordingTimer = Task {
                for second in 1...60 {
                    try? await Task.sleep(for: .seconds(1))
                    guard !Task.isCancelled, talking else { return }
                    recordingSeconds = second
                }
                finishRecording()
            }
        } catch {
            finishRecording(send: false)
            errorText = "录音失败：\(error.localizedDescription)"
        }
    }

    func finishRecording(send shouldSend: Bool = true) {
        captureEngine.stop()
        if inputTap { captureEngine.inputNode.removeTap(onBus: 0); inputTap = false }
        recordingTimer?.cancel(); recordingTimer = nil
        talking = false; inputLevel = 0
        let pcm = recordingPCM; recordingPCM.removeAll(keepingCapacity: true)
        message = hardware ? "按住开发板 Button 1 发送语音" : "设备未连接"
        guard shouldSend else { return }
        guard pcm.count >= 3200 else { errorText = "录音太短，请说完再发送"; return }
        let id = UUID().uuidString
        guard addVoice(id: id, source: "app", timestamp: Date().timeIntervalSince1970,
                       pcm: pcm, state: "sending") else { return }
        deviceBusy = true
        Task {
            do { try await send(["type": "voice_send", "id": id, "pcm": pcm.base64EncodedString()]) }
            catch {
                updateDelivery(id, state: "failed"); deviceBusy = false
                errorText = "发送失败：\(error.localizedDescription)"
            }
        }
    }

    func cancelRecording() { finishRecording(send: false) }

    @discardableResult
    private func addVoice(id: String, source: String, timestamp: Double, pcm: Data, state: String) -> Bool {
        guard !messages.contains(where: { $0.id == id }), pcm.count % 2 == 0,
              pcm.count >= 3200, pcm.count <= 1920000 else { return false }
        // Use UUID file names only; incoming message IDs never become arbitrary paths.
        guard UUID(uuidString: id) != nil else { return false }
        do {
            var wav = Data("RIFF".utf8)
            func u32(_ value: UInt32) { var n = value.littleEndian; withUnsafeBytes(of: &n) { wav.append(contentsOf: $0) } }
            func u16(_ value: UInt16) { var n = value.littleEndian; withUnsafeBytes(of: &n) { wav.append(contentsOf: $0) } }
            u32(UInt32(pcm.count + 36)); wav.append(Data("WAVEfmt ".utf8))
            u32(16); u16(1); u16(1); u32(16000); u32(32000); u16(2); u16(16)
            wav.append(Data("data".utf8)); u32(UInt32(pcm.count)); wav.append(pcm)
            try wav.write(to: audioURL(id), options: .atomic)
            messages.append(VoiceMessage(id: id, source: source, timestamp: timestamp,
                                         duration: Double(pcm.count) / 32000, state: state))
            saveHistory(); return true
        } catch { errorText = "保存语音失败：\(error.localizedDescription)"; return false }
    }

    private func audioURL(_ id: String) -> URL { directory.appendingPathComponent(id + ".wav") }
    private func saveHistory() {
        do { try JSONEncoder().encode(messages).write(to: directory.appendingPathComponent("history.json"), options: .atomic) }
        catch { errorText = "保存聊天记录失败：\(error.localizedDescription)" }
    }
    private func updateDelivery(_ id: String, state: String) {
        if let i = messages.firstIndex(where: { $0.id == id }) { messages[i].state = state; saveHistory() }
    }

    func stopPlayback() { player?.stop(); player = nil; playingID = nil }
    func playMessage(_ voice: VoiceMessage) {
        guard !talking else { return }
        if playingID == voice.id { stopPlayback(); return }
        do {
            stopPlayback()
            let session = AVAudioSession.sharedInstance()
            // Reset output routing after recording, so replay uses the simulator speaker.
            try session.setCategory(.playback, mode: .default)
            try session.setActive(true)
            let audio = try AVAudioPlayer(contentsOf: audioURL(voice.id))
            audio.delegate = self; audio.volume = 1
            guard audio.prepareToPlay(), audio.play() else { throw URLError(.cannotDecodeContentData) }
            player = audio; playingID = voice.id; errorText = ""
        } catch { errorText = "播放失败：\(error.localizedDescription)" }
    }
    nonisolated func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        Task { @MainActor in self.playingID = nil }
    }

    private func receive(_ text: String) {
        guard let data = text.data(using: .utf8),
              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let type = object["type"] as? String else { return }
        switch type {
        case "voice":
            guard let id = object["id"] as? String, let encoded = object["pcm"] as? String,
                  let pcm = Data(base64Encoded: encoded) else { return }
            addVoice(id: id, source: "device", timestamp: object["timestamp"] as? Double ?? Date().timeIntervalSince1970,
                     pcm: pcm, state: "received")
        case "device_recording": deviceRecording = object["enabled"] as? Bool ?? false
        case "delivery":
            guard let id = object["id"] as? String, let state = object["state"] as? String else { return }
            updateDelivery(id, state: state)
            deviceBusy = state == "playing"
        case "error": errorText = object["message"] as? String ?? "设备错误"; deviceBusy = false
        case "status":
            connected = true; hardware = object["hardware_connected"] as? Bool ?? false
            deviceRecording = object["device_recording"] as? Bool ?? false
            deviceBusy = object["talking"] as? Bool ?? false
            boardErrors = (object["board"] as? [String: Any])?["errors"] as? Int ?? 0
            if !hardware && talking { cancelRecording() }
            if !talking { message = hardware ? "按住开发板 Button 1 发送语音" : "桥接已连接，等待开发板" }
        default: break
        }
    }
}
