import Foundation
import CoreAudio

func check(_ status: OSStatus, _ action: String) {
    if status != noErr { fputs("\(action): \(status)\n", stderr); exit(1) }
}
var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
var size: UInt32 = 0
check(AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size), "Enumerate devices")
var devices = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
check(AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &devices), "Read devices")
for device in devices {
    var nameAddress = AudioObjectPropertyAddress(mSelector: kAudioObjectPropertyName, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var name: CFString = "" as CFString
    var nameSize = UInt32(MemoryLayout<CFString>.size)
    check(AudioObjectGetPropertyData(device, &nameAddress, 0, nil, &nameSize, &name), "Read name")
    if name as String == "CAT USB Speaker" {
        var selected = device
        var outputAddress = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultOutputDevice, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        check(AudioObjectSetPropertyData(AudioObjectID(kAudioObjectSystemObject), &outputAddress, 0, nil, UInt32(MemoryLayout<AudioDeviceID>.size), &selected), "Set output")
        print("Default output: CAT USB Speaker (\(device))")
        exit(0)
    }
}
fputs("CAT USB Speaker not found. Connect nRF USB (J3).\n", stderr)
exit(1)
